# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# TODO: 2024-04-11 The module contains too many lines which are scheduled for refactoring.
# pylint: disable=too-many-lines

# TODO: 2024-04-22 The module contains duplicate code which is scheduled for refactoring.
# Lines related to issuing metrics are duplicated:
#  ==openstack_cloud.openstack_manager:[1320:1337]
#  ==runner_manager:[383:413]
#  ==openstack_cloud.openstack_manager:[1283:1314]
#  ==runner_manager:[339:368]

# pylint: disable=duplicate-code

"""Module for handling interactions with OpenStack."""
import logging
import secrets
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from multiprocessing import Pool
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional, cast

import invoke
import jinja2
import openstack
import openstack.connection
import openstack.exceptions
import openstack.image.v2.image
import paramiko
from fabric import Connection as SshConnection
from invoke.runners import Result
from openstack.compute.v2.server import Server
from openstack.connection import Connection as OpenstackConnection
from openstack.exceptions import SDKException
from openstack.network.v2.security_group import SecurityGroup
from paramiko.ssh_exception import NoValidConnectionsError

import reactive.runner_manager as reactive_runner_manager
from charm_state import CharmState, GithubOrg, ProxyConfig, SSHDebugConnection
from errors import (
    CreateMetricsStorageError,
    GetMetricsStorageError,
    GithubApiError,
    GithubClientError,
    GithubMetricsError,
    IssueMetricEventError,
    OpenStackError,
    RunnerCreateError,
    RunnerStartError,
)
from github_client import GithubClient
from github_type import GitHubRunnerStatus, SelfHostedRunner
from metrics import events as metric_events
from metrics import github as github_metrics
from metrics import runner as runner_metrics
from metrics import storage as metrics_storage
from metrics.runner import RUNNER_INSTALLED_TS_FILE_NAME
from repo_policy_compliance_client import RepoPolicyComplianceClient
from runner_manager import IssuedMetricEventsStats
from runner_manager_type import OpenstackRunnerManagerConfig
from runner_type import GithubPath, RunnerByHealth, RunnerGithubInfo
from utilities import retry, set_env_var

logger = logging.getLogger(__name__)

# Update the version when the security group rules are not backward compatible.
SECURITY_GROUP_NAME = "github-runner-v1"
BUILD_OPENSTACK_IMAGE_SCRIPT_FILENAME = "scripts/build-openstack-image.sh"
_SSH_KEY_PATH = Path("/home/ubuntu/.ssh")
_CONFIG_SCRIPT_PATH = Path("/home/ubuntu/actions-runner/config.sh")

RUNNER_APPLICATION = Path("/home/ubuntu/actions-runner")
METRICS_EXCHANGE_PATH = Path("/home/ubuntu/metrics-exchange")
PRE_JOB_SCRIPT = RUNNER_APPLICATION / "pre-job.sh"
MAX_METRICS_FILE_SIZE = 1024

RUNNER_STARTUP_PROCESS = "/home/ubuntu/actions-runner/run.sh"
RUNNER_LISTENER_PROCESS = "Runner.Listener"
RUNNER_WORKER_PROCESS = "Runner.Worker"
CREATE_SERVER_TIMEOUT = 5 * 60


class _PullFileError(Exception):
    """Represents an error while pulling a file from the runner instance."""

    def __init__(self, reason: str):
        """Construct PullFileError object.

        Args:
            reason: The reason for the error.
        """
        super().__init__(reason)


class _SSHError(Exception):
    """Represents an error while interacting with SSH."""

    def __init__(self, reason: str):
        """Construct SSHErrors object.

        Args:
            reason: The reason for the error.
        """
        super().__init__(reason)


@dataclass
class InstanceConfig:
    """The configuration values for creating a single runner instance.

    Attributes:
        github_path: The GitHub repo/org path to register the runner.
        image_id: The Openstack image id to use to boot the instance with.
        labels: The runner instance labels.
        name: Name of the image to launch the GitHub runner instance with.
        registration_token: Token for registering the runner on GitHub.
    """

    github_path: GithubPath
    image_id: str
    labels: Iterable[str]
    name: str
    registration_token: str


SupportedCloudImageArch = Literal["amd64", "arm64"]


@dataclass
class _CloudInitUserData:
    """Dataclass to hold cloud init userdata.

    Attributes:
        instance_config: The configuration values for Openstack instance to launch.
        runner_env: The contents of .env to source when launching Github runner.
        pre_job_contents: The contents of pre-job script to run before starting the job.
        proxies: Proxy values to enable on the Github runner.
        dockerhub_mirror: URL to dockerhub mirror.
    """

    instance_config: InstanceConfig
    runner_env: str
    pre_job_contents: str
    dockerhub_mirror: Optional[str] = None
    proxies: Optional[ProxyConfig] = None


@contextmanager
def _create_connection(cloud_config: dict[str, dict]) -> Iterator[openstack.connection.Connection]:
    """Create a connection context managed object, to be used within with statements.

    This method should be called with a valid cloud_config. See _validate_cloud_config.
    Also, this method assumes that the clouds.yaml exists on ~/.config/openstack/clouds.yaml.
    See charm_state.py _write_openstack_config_to_disk.

    Args:
        cloud_config: The configuration in clouds.yaml format to apply.

    Raises:
        OpenStackError: if the credentials provided is not authorized.

    Yields:
        An openstack.connection.Connection object.
    """
    clouds = list(cloud_config["clouds"].keys())
    if len(clouds) > 1:
        logger.warning("Multiple clouds defined in clouds.yaml. Using the first one to connect.")
    cloud_name = clouds[0]

    # api documents that keystoneauth1.exceptions.MissingRequiredOptions can be raised but
    # I could not reproduce it. Therefore, no catch here for such exception.
    try:
        with openstack.connect(cloud=cloud_name) as conn:
            conn.authorize()
            yield conn
    # pylint thinks this isn't an exception, but does inherit from Exception class.
    except openstack.exceptions.HttpException as exc:  # pylint: disable=bad-exception-cause
        logger.exception("OpenStack API call failure")
        raise OpenStackError("Failed OpenStack API call") from exc


# Disable too many arguments, as they are needed to create the dataclass.
def create_instance_config(  # pylint: disable=too-many-arguments
    app_name: str,
    unit_num: int,
    image_id: str,
    path: GithubPath,
    labels: Iterable[str],
    registration_token: str,
) -> InstanceConfig:
    """Create an instance config from charm data.

    Args:
        app_name: The juju application name.
        unit_num: The juju unit number.
        image_id: The openstack image id to create the instance with.
        path: Github organisation or repository path.
        labels: Addition labels for the runner.
        registration_token: The Github runner registration token. See \
            https://docs.github.com/en/rest/actions/self-hosted-runners?apiVersion=2022-11-28#create-a-registration-token-for-a-repository

    Returns:
        Instance configuration created.
    """
    suffix = secrets.token_hex(12)
    return InstanceConfig(
        github_path=path,
        image_id=image_id,
        labels=labels,
        name=f"{app_name}-{unit_num}-{suffix}",
        registration_token=registration_token,
    )


def _generate_runner_env(
    templates_env: jinja2.Environment,
    dockerhub_mirror: Optional[str] = None,
    ssh_debug_connections: list[SSHDebugConnection] | None = None,
) -> str:
    """Generate Github runner .env file contents.

    Proxy configuration are handled by aproxy.

    Args:
        templates_env: The jinja template environment.
        dockerhub_mirror: The url to Dockerhub to reduce rate limiting.
        ssh_debug_connections: Tmate SSH debug connection information to load as environment vars.

    Returns:
        The .env contents to be loaded by Github runner.
    """
    return templates_env.get_template("env.j2").render(
        pre_job_script=str(PRE_JOB_SCRIPT),
        dockerhub_mirror=dockerhub_mirror or "",
        ssh_debug_info=(secrets.choice(ssh_debug_connections) if ssh_debug_connections else None),
        # Proxies are handled by aproxy.
        proxies={},
    )


def _generate_cloud_init_userdata(
    templates_env: jinja2.Environment,
    cloud_init_userdata: _CloudInitUserData,
) -> str:
    """Generate cloud init userdata to launch at startup.

    Args:
        templates_env: The jinja template environment.
        cloud_init_userdata: The dataclass containing the cloud init userdata.

    Returns:
        The cloud init userdata script.
    """
    runner_group = None
    instance_config = cloud_init_userdata.instance_config
    proxies = cloud_init_userdata.proxies

    if isinstance(instance_config.github_path, GithubOrg):
        runner_group = instance_config.github_path.group

    aproxy_address = proxies.aproxy_address if proxies is not None else None
    return templates_env.get_template("openstack-userdata.sh.j2").render(
        github_url=f"https://github.com/{instance_config.github_path.path()}",
        runner_group=runner_group,
        token=instance_config.registration_token,
        instance_labels=",".join(instance_config.labels),
        instance_name=instance_config.name,
        env_contents=cloud_init_userdata.runner_env,
        pre_job_contents=cloud_init_userdata.pre_job_contents,
        metrics_exchange_path=str(METRICS_EXCHANGE_PATH),
        aproxy_address=aproxy_address,
        dockerhub_mirror=cloud_init_userdata.dockerhub_mirror,
    )


class GithubRunnerRemoveError(Exception):
    """Represents an error removing registered runner from Github."""


_INSTANCE_STATUS_SHUTOFF = "SHUTOFF"
_INSTANCE_STATUS_ERROR = "ERROR"
_INSTANCE_STATUS_ACTIVE = "ACTIVE"
_INSTANCE_STATUS_BUILDING = "BUILDING"


class OpenstackRunnerManager:
    """Runner manager for OpenStack-based instances.

    Attributes:
        app_name: The juju application name.
        unit_num: The juju unit number.
        instance_name: Prefix of the name for the set of runners.
    """

    def __init__(
        self,
        app_name: str,
        unit_num: int,
        openstack_runner_manager_config: OpenstackRunnerManagerConfig,
        cloud_config: dict[str, dict],
    ):
        """Construct OpenstackRunnerManager object.

        Args:
            app_name: The juju application name.
            unit_num: The juju unit number.
            openstack_runner_manager_config: Configurations related to runner manager.
            cloud_config: The openstack clouds.yaml in dict format.
        """
        # Setting the env var to this process and any child process spawned.
        proxies = openstack_runner_manager_config.charm_state.proxy_config
        if no_proxy := proxies.no_proxy:
            set_env_var("NO_PROXY", no_proxy)
        if http_proxy := proxies.http:
            set_env_var("HTTP_PROXY", http_proxy)
        if https_proxy := proxies.https:
            set_env_var("HTTPS_PROXY", https_proxy)

        self.app_name = app_name
        self.unit_num = unit_num
        self.instance_name = f"{app_name}-{unit_num}"
        self._config = openstack_runner_manager_config
        self._cloud_config = cloud_config
        self._github = GithubClient(token=self._config.token)

    def reconcile(self, quantity: int) -> int:
        """Reconcile the quantity of runners.

        Args:
            quantity: The number of intended runners.

        Returns:
            The change in number of runners.
        """
        if self._config.reactive_config:
            logger.info("Reactive configuration detected, going into experimental reactive mode.")
            return self._reconcile_reactive(quantity)

        start_ts = time.time()
        try:
            delta = self._reconcile_runners(quantity)
        finally:
            end_ts = time.time()
            self._issue_reconciliation_metrics(
                reconciliation_start_ts=start_ts, reconciliation_end_ts=end_ts
            )

        return delta

    def _reconcile_reactive(self, quantity: int) -> int:
        """Reconcile runners reactively.

        Args:
            quantity: Number of intended runners.

        Returns:
            The difference between intended runners and actual runners. In reactive mode
            this number is never negative as additional processes should terminate after a timeout.
        """
        logger.info("Reactive mode is experimental and not yet fully implemented.")
        return reactive_runner_manager.reconcile(
            quantity=quantity, mq_uri=self._config.reactive_config.mq_uri, queue_name=self.app_name
        )

    def _reconcile_runners(self, quantity: int) -> int:
        """Reconcile the number of runners.

        Args:
            quantity: The number of intended runners.

        Returns:
            The change in number of runners.
        """
        with _create_connection(self._cloud_config) as conn:
            runner_by_health = self._get_openstack_runner_status(conn)
            logger.info(
                "Found %s healthy runner and %s unhealthy runner",
                len(runner_by_health.healthy),
                len(runner_by_health.unhealthy),
            )
            logger.debug("Healthy runner: %s", runner_by_health.healthy)
            logger.debug("Unhealthy runner: %s", runner_by_health.unhealthy)
            remove_token = self._github.get_runner_remove_token(path=self._config.path)

            self._clean_up_runners(
                conn=conn, runner_by_health=runner_by_health, remove_token=remove_token
            )

            delta = self._scale(
                quantity=quantity,
                conn=conn,
                runner_by_health=runner_by_health,
                remove_token=remove_token,
            )
        return delta

    def get_github_runner_info(self) -> tuple[RunnerGithubInfo, ...]:
        """Get information on GitHub for the runners.

        Returns:
            Collection of runner GitHub information.
        """
        remote_runners_list: list[SelfHostedRunner] = self._github.get_runner_github_info(
            self._config.path
        )
        logger.debug("List of runners found on GitHub:%s", remote_runners_list)
        return tuple(
            RunnerGithubInfo(
                runner["name"],
                runner["id"],
                runner["status"] == GitHubRunnerStatus.ONLINE,
                runner["busy"],
            )
            for runner in remote_runners_list
            if runner["name"].startswith(f"{self.instance_name}-")
        )

    def _get_openstack_runner_status(self, conn: OpenstackConnection) -> RunnerByHealth:
        """Get status on OpenStack of each runner.

        Args:
            conn: The connection object to access OpenStack cloud.

        Returns:
            Runner status grouped by health.
        """
        healthy_runner = []
        unhealthy_runner = []
        openstack_instances = self._get_openstack_instances(conn)

        logger.debug("Found openstack instances: %s", openstack_instances)

        for instance in openstack_instances:
            if not OpenstackRunnerManager._health_check(conn=conn, server_name=instance.name):
                unhealthy_runner.append(instance.name)
            else:
                healthy_runner.append(instance.name)

        return RunnerByHealth(healthy=tuple(healthy_runner), unhealthy=tuple(unhealthy_runner))

    def _get_openstack_instances(self, conn: OpenstackConnection) -> list[Server]:
        """Get the OpenStack servers managed by this unit.

        Args:
            conn: The connection object to access OpenStack cloud.

        Returns:
            List of OpenStack instances.
        """
        return [
            instance
            for instance in cast(list[Server], conn.list_servers())
            if instance.name.startswith(f"{self.instance_name}-")
        ]

    @staticmethod
    def _health_check(conn: OpenstackConnection, server_name: str, startup: bool = False) -> bool:
        """Health check a server instance.

        A healthy server is defined as:
            1. Openstack instance status is ACTIVE or BUILDING.
            2. Runner.Worker exists (running a job).
            3. Runner.Listener exists (waiting for job).
            3. GitHub runner status is Idle or Active.

        An undetermined server is marked as healthy when:
            1. SSH fails - could be a transient network error.
            2. The Runner.* processes do not exist. Mark healthy for now to gather data. This is
                subject to change to unhealthy once enough data has been gathered.

        Args:
            conn: The Openstack connection instance.
            server_name: The name of the OpenStack server to health check.
            startup: Check only whether the startup is successful.

        Returns:
            Whether the instance is healthy.
        """
        server: Server | None = conn.get_server(name_or_id=server_name)
        if not server:
            return False
        if server.status == (_INSTANCE_STATUS_SHUTOFF, _INSTANCE_STATUS_ERROR):
            return False
        if server.status not in (_INSTANCE_STATUS_ACTIVE, _INSTANCE_STATUS_BUILDING):
            return False
        return OpenstackRunnerManager._ssh_health_check(
            conn=conn, server_name=server_name, startup=startup
        )

    @staticmethod
    def _ssh_health_check(conn: OpenstackConnection, server_name: str, startup: bool) -> bool:
        """Use SSH to check whether runner application is running.

        A healthy runner is defined as:
            1. SSH connection can be established.
            2. Runner.Worker exists (running a job).
            3. Runner.Listener exists (waiting for job).

        Args:
            conn: The Openstack connection instance.
            server_name: The openstack server instance to check connections.
            startup: Check only whether the startup is successful.

        Returns:
            Whether the runner application is running.
        """
        try:
            ssh_conn = OpenstackRunnerManager._get_ssh_connection(
                conn=conn, server_name=server_name
            )
        except _SSHError as exc:
            logger.error("[ALERT]: Unable to SSH to server: %s, reason: %s", server_name, str(exc))
            return True

        result: invoke.runners.Result = ssh_conn.run("ps aux", warn=True)
        logger.debug("Output of `ps aux` on %s stderr: %s", server_name, result.stderr)
        if not result.ok or RUNNER_STARTUP_PROCESS not in result.stdout:
            logger.warning("List all process command failed on %s ", server_name)
            return False
        logger.info("Runner process found to be healthy on %s", server_name)
        if startup:
            return True

        if RUNNER_WORKER_PROCESS in result.stdout or RUNNER_LISTENER_PROCESS in result.stdout:
            return True

        logger.error("[ALERT] Health check failed for server: %s", server_name)
        return True

    @staticmethod
    @retry(tries=3, delay=5, max_delay=60, backoff=2, local_logger=logger)
    def _get_ssh_connection(
        conn: OpenstackConnection, server_name: str, timeout: int = 30
    ) -> SshConnection:
        """Get a valid ssh connection within a network for a given openstack instance.

        The SSH connection will attempt to establish connection until the timeout configured.

        Args:
            conn: The Openstack connection instance.
            server_name: The Openstack server instance name.
            timeout: Timeout in seconds to attempt connection to each available server address.

        Raises:
            _SSHError: If there was an error getting a valid SSH connection.

        Returns:
            An SSH connection to OpenStack server instance.
        """
        server: Server | None = conn.get_server(name_or_id=server_name)
        if server is None:
            raise _SSHError(f"Server gone while trying to get SSH connection: {server_name}.")
        if not server.key_name:
            raise _SSHError(
                f"Unable to create SSH connection, no valid keypair found for {server.name}"
            )
        key_path = OpenstackRunnerManager._get_key_path(server.name)
        if not key_path.exists():
            raise _SSHError(f"Missing keyfile for server: {server.name}, key path: {key_path}")
        network_address_list = server.addresses.values()
        if not network_address_list:
            raise _SSHError(f"No addresses found for OpenStack server {server.name}")

        server_addresses: list[str] = [
            address["addr"]
            for network_addresses in network_address_list
            for address in network_addresses
        ]
        for ip in server_addresses:
            try:
                connection = SshConnection(
                    host=ip,
                    user="ubuntu",
                    connect_kwargs={"key_filename": str(key_path)},
                    connect_timeout=timeout,
                )
                result = connection.run("echo hello world", warn=True, timeout=timeout)
                if not result.ok:
                    logger.warning(
                        "SSH test connection failed, server: %s, address: %s", server.name, ip
                    )
                    continue
                if "hello world" in result.stdout:
                    return connection
            except (NoValidConnectionsError, TimeoutError, paramiko.ssh_exception.SSHException):
                logger.warning(
                    "Unable to SSH into %s with address %s",
                    server.name,
                    connection.host,
                    exc_info=True,
                )
                continue
        raise _SSHError(
            f"No connectable SSH addresses found, server: {server.name}, "
            f"addresses: {server_addresses}"
        )

    @staticmethod
    def _get_key_path(name: str) -> Path:
        """Get the filepath for storing private SSH of a runner.

        Args:
            name: The name of the runner.

        Returns:
            Path to reserved for the key file of the runner.
        """
        return _SSH_KEY_PATH / f"runner-{name}.key"

    @dataclass
    class _CreateRunnerArgs:
        """Arguments for _create_runner method.

        Attributes:
            app_name: The juju application name.
            cloud_config: The clouds.yaml containing the OpenStack credentials. The first cloud
                in the file will be used.
            config: Configurations related to runner manager.
            registration_token: Token for registering the runner on GitHub.
            unit_num: The juju unit number.
        """

        app_name: str
        cloud_config: dict[str, dict]
        config: OpenstackRunnerManagerConfig
        registration_token: str
        unit_num: int

    @staticmethod
    def _create_runner(args: _CreateRunnerArgs) -> None:
        """Create a runner on OpenStack cloud.

        Arguments are gathered into a dataclass due to Pool.map needing one argument functions.

        Args:
            args: Arguments of the method.

        Raises:
            RunnerCreateError: Unable to create the OpenStack runner.
        """
        ts_now = time.time()
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader("templates"), autoescape=True
        )

        env_contents = _generate_runner_env(
            templates_env=environment,
            dockerhub_mirror=args.config.dockerhub_mirror,
            ssh_debug_connections=args.config.charm_state.ssh_debug_connections,
        )

        pre_job_contents = OpenstackRunnerManager._render_pre_job_contents(
            charm_state=args.config.charm_state, templates_env=environment
        )

        instance_config = create_instance_config(
            args.app_name,
            args.unit_num,
            args.config.image,
            args.config.path,
            args.config.labels,
            args.registration_token,
        )
        cloud_user_data = _CloudInitUserData(
            instance_config=instance_config,
            runner_env=env_contents,
            pre_job_contents=pre_job_contents,
            dockerhub_mirror=args.config.dockerhub_mirror,
            proxies=args.config.charm_state.proxy_config,
        )
        cloud_userdata_str = _generate_cloud_init_userdata(
            templates_env=environment,
            cloud_init_userdata=cloud_user_data,
        )

        with _create_connection(cloud_config=args.cloud_config) as conn:
            runner_security_group = OpenstackRunnerManager._ensure_security_group(conn)
            OpenstackRunnerManager._setup_runner_keypair(conn, instance_config.name)

            logger.info("Creating runner %s", instance_config.name)
            try:
                instance = conn.create_server(
                    name=instance_config.name,
                    image=instance_config.image_id,
                    key_name=instance_config.name,
                    flavor=args.config.flavor,
                    network=args.config.network,
                    security_groups=[runner_security_group["id"]],
                    userdata=cloud_userdata_str,
                    auto_ip=False,
                    timeout=CREATE_SERVER_TIMEOUT,
                    wait=True,
                )
            except openstack.exceptions.ResourceTimeout as err:
                logger.exception("Timeout creating OpenStack runner %s", instance_config.name)
                try:
                    logger.info(
                        "Attempting to remove OpenStack runner %s that timeout on creation",
                        instance_config.name,
                    )
                    conn.delete_server(name_or_id=instance_config.name, wait=True)
                    try:
                        conn.delete_keypair(instance_config.name)
                    except openstack.exceptions.SDKException:
                        logger.exception(
                            "Unable to delete OpenStack keypair %s", instance_config.name
                        )
                    OpenstackRunnerManager._get_key_path(instance_config.name).unlink(
                        missing_ok=True
                    )
                except openstack.exceptions.SDKException:
                    logger.exception(
                        "Cleanup of creation failure runner %s has failed", instance_config.name
                    )
                    # Reconcile will attempt to cleanup again prior to spawning new runners.
                raise RunnerCreateError(
                    f"Timeout creating OpenStack runner {instance_config.name}"
                ) from err
            except openstack.exceptions.SDKException as err:
                logger.exception("Failed to create OpenStack runner %s", instance_config.name)
                raise RunnerCreateError(
                    f"Failed to create OpenStack runner {instance_config.name}"
                ) from err

            logger.info("Waiting runner %s to come online", instance_config.name)
            OpenstackRunnerManager._wait_until_runner_process_running(conn, instance.name)
            logger.info("Finished creating runner %s", instance_config.name)
        ts_after = time.time()
        OpenstackRunnerManager._issue_runner_installed_metric(
            app_name=args.app_name,
            instance_config=instance_config,
            install_end_ts=ts_after,
            install_start_ts=ts_now,
        )

    @staticmethod
    def _render_pre_job_contents(
        charm_state: CharmState, templates_env: jinja2.Environment
    ) -> str:
        """Render the pre-job script contents.

        Args:
            charm_state: The charm state object.
            templates_env: The jinja template environment.

        Returns:
            The rendered pre-job script contents.
        """
        pre_job_contents_dict = {
            "issue_metrics": True,
            "metrics_exchange_path": str(METRICS_EXCHANGE_PATH),
            "do_repo_policy_check": False,
        }
        if repo_policy_config := charm_state.charm_config.repo_policy_compliance:
            repo_policy_client = RepoPolicyComplianceClient(
                url=repo_policy_config.url, charm_token=repo_policy_config.token
            )
            pre_job_contents_dict.update(
                {
                    "repo_policy_base_url": repo_policy_client.base_url,
                    "repo_policy_one_time_token": repo_policy_client.get_one_time_token(),
                    "do_repo_policy_check": True,
                }
            )
        pre_job_contents = templates_env.get_template("pre-job.j2").render(pre_job_contents_dict)
        return pre_job_contents

    @staticmethod
    def _ensure_security_group(conn: OpenstackConnection) -> SecurityGroup:
        """Ensure runner security group exists.

        Args:
            conn: The connection object to access OpenStack cloud.

        Returns:
            The security group with the rules for runners.
        """
        rule_exists_icmp = False
        rule_exists_ssh = False
        rule_exists_tmate_ssh = False

        security_group_list = conn.list_security_groups(filters={"name": SECURITY_GROUP_NAME})
        # Pick the first security_group returned.
        security_group = next(iter(security_group_list), None)

        if security_group is None:
            logger.info("Security group %s not found, creating it", SECURITY_GROUP_NAME)
            security_group = conn.create_security_group(
                name=SECURITY_GROUP_NAME,
                description="For servers managed by the github-runner charm.",
            )
        else:
            existing_rules = security_group["security_group_rules"]
            for rule in existing_rules:
                if rule["protocol"] == "icmp":
                    logger.debug(
                        "Found ICMP rule in existing security group %s of ID %s",
                        SECURITY_GROUP_NAME,
                        security_group["id"],
                    )
                    rule_exists_icmp = True
                if (
                    rule["protocol"] == "tcp"
                    and rule["port_range_min"] == rule["port_range_max"] == 22
                ):
                    logger.debug(
                        "Found SSH rule in existing security group %s of ID %s",
                        SECURITY_GROUP_NAME,
                        security_group["id"],
                    )
                    rule_exists_ssh = True
                if (
                    rule["protocol"] == "tcp"
                    and rule["port_range_min"] == rule["port_range_max"] == 10022
                ):
                    logger.debug(
                        "Found tmate SSH rule in existing security group %s of ID %s",
                        SECURITY_GROUP_NAME,
                        security_group["id"],
                    )
                    rule_exists_tmate_ssh = True

        if not rule_exists_icmp:
            conn.create_security_group_rule(
                secgroup_name_or_id=security_group["id"],
                protocol="icmp",
                direction="ingress",
                ethertype="IPv4",
            )
        if not rule_exists_ssh:
            conn.create_security_group_rule(
                secgroup_name_or_id=security_group["id"],
                port_range_min="22",
                port_range_max="22",
                protocol="tcp",
                direction="ingress",
                ethertype="IPv4",
            )
        if not rule_exists_tmate_ssh:
            conn.create_security_group_rule(
                secgroup_name_or_id=security_group["id"],
                port_range_min="10022",
                port_range_max="10022",
                protocol="tcp",
                direction="egress",
                ethertype="IPv4",
            )
        return security_group

    @staticmethod
    def _setup_runner_keypair(conn: OpenstackConnection, name: str) -> None:
        """Set up the SSH keypair for a runner.

        Args:
            conn: The connection object to access OpenStack cloud.
            name: The name of the runner.
        """
        private_key_path = OpenstackRunnerManager._get_key_path(name)

        if private_key_path.exists():
            logger.warning("Existing private key file for %s found, removing it.", name)
            private_key_path.unlink()

        keypair = conn.create_keypair(name=name)
        private_key_path.write_text(keypair.private_key)
        shutil.chown(private_key_path, user="ubuntu", group="ubuntu")
        private_key_path.chmod(0o400)

    @retry(tries=10, delay=60, local_logger=logger)
    @staticmethod
    def _wait_until_runner_process_running(conn: OpenstackConnection, instance_name: str) -> None:
        """Wait until the runner process is running.

        The waiting to done by the retry declarator.

        Args:
            conn: The openstack connection instance.
            instance_name: The name of the instance to wait on.

        Raises:
            RunnerStartError: Unable perform health check of the runner application.
        """
        try:
            if not OpenstackRunnerManager._health_check(
                conn=conn, server_name=instance_name, startup=True
            ):
                raise RunnerStartError(
                    (
                        "Unable to find running process of runner application on openstack runner "
                        f"{instance_name}"
                    )
                )
        except TimeoutError as err:
            raise RunnerStartError(
                f"Unable to connect to openstack runner {instance_name}"
            ) from err

    @staticmethod
    def _issue_runner_installed_metric(
        app_name: str,
        instance_config: InstanceConfig,
        install_start_ts: float,
        install_end_ts: float,
    ) -> None:
        """Issue RunnerInstalled metric.

        Args:
            app_name: The juju application name.
            instance_config: The configuration values for Openstack instance.
            install_start_ts: The timestamp when the installation started.
            install_end_ts: The timestamp when the installation ended.
        """
        try:
            metric_events.issue_event(
                event=metric_events.RunnerInstalled(
                    timestamp=install_start_ts,
                    flavor=app_name,
                    duration=install_end_ts - install_start_ts,
                ),
            )
        except IssueMetricEventError:
            logger.exception("Failed to issue RunnerInstalled metric")
        try:
            storage = metrics_storage.create(instance_config.name)
        except CreateMetricsStorageError:
            logger.exception(
                "Failed to create metrics storage for runner %s, "
                "will not be able to issue all metrics.",
                instance_config.name,
            )
        else:
            try:
                (storage.path / RUNNER_INSTALLED_TS_FILE_NAME).write_text(
                    str(install_end_ts), encoding="utf-8"
                )
            except FileNotFoundError:
                logger.exception(
                    "Failed to write runner-installed.timestamp into metrics storage "
                    "for runner %s, will not be able to issue all metrics.",
                    instance_config.name,
                )

    def _remove_runners(
        self,
        conn: OpenstackConnection,
        instance_names: Iterable[str],
        remove_token: str | None = None,
        num_to_remove: int | float | None = None,
    ) -> None:
        """Delete runners on Openstack.

        Removes the registered runner from Github if remove_token is provided.

        Args:
            conn: The Openstack connection instance.
            instance_names: The Openstack server names to delete.
            remove_token: The GitHub runner remove token.
            num_to_remove: Remove a specified number of runners. Remove all if None.
        """
        if num_to_remove is None:
            num_to_remove = float("inf")

        name_to_github_id = {
            runner["name"]: runner["id"]
            for runner in self._github.get_runner_github_info(self._config.path)
        }
        for instance_name in instance_names:
            if num_to_remove < 1:
                break

            github_id = name_to_github_id.get(instance_name, None)
            self._remove_one_runner(conn, instance_name, github_id, remove_token)

            # Attempt to delete the keys. This is place at the end of deletion, so we can access
            # the instances that failed to delete on previous tries.
            try:
                conn.delete_keypair(instance_name)
            except openstack.exceptions.SDKException:
                logger.exception("Unable to delete OpenStack keypair %s", instance_name)
            OpenstackRunnerManager._get_key_path(instance_name).unlink(missing_ok=True)
            num_to_remove -= 1

    def _remove_one_runner(
        self,
        conn: OpenstackConnection,
        instance_name: str,
        github_id: int | None = None,
        remove_token: str | None = None,
    ) -> None:
        """Remove one OpenStack runner.

        Args:
            conn: The Openstack connection instance.
            instance_name: The Openstack server name to delete.
            github_id: The runner id on GitHub.
            remove_token: The GitHub runner remove token.
        """
        logger.info("Attempting to remove OpenStack runner %s", instance_name)

        server: Server | None = conn.get_server(name_or_id=instance_name)

        if server is not None:
            logger.info(
                "Pulling metrics and deleting server for OpenStack runner %s", instance_name
            )
            self._pull_metrics(conn=conn, instance_name=instance_name)
            self._remove_openstack_runner(conn, server, remove_token)
        else:
            logger.info(
                "Not found server for OpenStack runner %s marked for deletion", instance_name
            )

        if github_id is not None:
            try:
                self._github.delete_runner(self._config.path, github_id)
            except GithubClientError as exc:
                logger.warning("Failed to remove runner from Github %s, %s", instance_name, exc)
            # TODO: 2024-04-23: The broad except clause is for logging purposes.
            # Will be removed in future versions.
            except Exception:  # pylint: disable=broad-exception-caught
                logger.critical(
                    "Found unexpected exception, please contact the developers", exc_info=True
                )

    def _pull_metrics(self, conn: OpenstackConnection, instance_name: str) -> None:
        """Pull metrics from the runner into the respective storage for the runner.

        Args:
            conn: The Openstack connection instance.
            instance_name: The Openstack server name.
        """
        try:
            storage = metrics_storage.get(instance_name)
        except GetMetricsStorageError:
            logger.exception(
                "Failed to get shared metrics storage for runner %s, "
                "will not be able to issue all metrics.",
                instance_name,
            )
            return

        try:
            ssh_conn = self._get_ssh_connection(conn=conn, server_name=instance_name)
        except _SSHError as exc:
            logger.info("Failed to pull metrics for %s: %s", instance_name, exc)
            return

        try:
            self._pull_file(
                ssh_conn=ssh_conn,
                remote_path=str(METRICS_EXCHANGE_PATH / "pre-job-metrics.json"),
                local_path=str(storage.path / "pre-job-metrics.json"),
                max_size=MAX_METRICS_FILE_SIZE,
            )
            self._pull_file(
                ssh_conn=ssh_conn,
                remote_path=str(METRICS_EXCHANGE_PATH / "post-job-metrics.json"),
                local_path=str(storage.path / "post-job-metrics.json"),
                max_size=MAX_METRICS_FILE_SIZE,
            )
            return
        except _PullFileError as exc:
            logger.warning(
                "Failed to pull metrics for %s: %s . Will not be able to issue all metrics",
                instance_name,
                exc,
            )
            return

    def _pull_file(
        self, ssh_conn: SshConnection, remote_path: str, local_path: str, max_size: int
    ) -> None:
        """Pull file from the runner instance.

        Args:
            ssh_conn: The SSH connection instance.
            remote_path: The file path on the runner instance.
            local_path: The local path to store the file.
            max_size: If the file is larger than this, it will not be pulled.

        Raises:
            _PullFileError: Unable to pull the file from the runner instance.
            _SSHError: Issue with SSH connection.
        """
        try:
            result = ssh_conn.run(f"stat -c %s {remote_path}", warn=True)
        except (NoValidConnectionsError, TimeoutError, paramiko.ssh_exception.SSHException) as exc:
            raise _SSHError(reason=f"Unable to SSH into {ssh_conn.host}") from exc
        if not result.ok:
            logger.warning(
                (
                    "Unable to get file size of %s on instance %s, "
                    "exit code: %s, stdout: %s, stderr: %s"
                ),
                remote_path,
                ssh_conn.host,
                result.return_code,
                result.stdout,
                result.stderr,
            )
            raise _PullFileError(reason=f"Unable to get file size of {remote_path}")

        stdout = result.stdout
        try:
            stdout.strip()
            size = int(stdout)
            if size > max_size:
                raise _PullFileError(
                    reason=f"File size of {remote_path} too large {size} > {max_size}"
                )
        except ValueError as exc:
            raise _PullFileError(reason=f"Invalid file size for {remote_path}: {stdout}") from exc

        try:
            ssh_conn.get(remote=remote_path, local=local_path)
        except (NoValidConnectionsError, TimeoutError, paramiko.ssh_exception.SSHException) as exc:
            raise _SSHError(reason=f"Unable to SSH into {ssh_conn.host}") from exc
        except OSError as exc:
            raise _PullFileError(reason=f"Unable to retrieve file {remote_path}") from exc

    def _remove_openstack_runner(
        self,
        conn: OpenstackConnection,
        server: Server,
        remove_token: str | None = None,
    ) -> None:
        """Remove a OpenStack server hosting the GitHub runner application.

        Args:
            conn: The Openstack connection instance.
            server: The Openstack server.
            remove_token: The GitHub runner remove token.
        """
        try:
            self._run_github_removal_script(conn=conn, server=server, remove_token=remove_token)
        except (TimeoutError, invoke.exceptions.UnexpectedExit, GithubRunnerRemoveError):
            logger.warning(
                "Failed to run runner removal script for %s", server.name, exc_info=True
            )
        # TODO: 2024-04-23: The broad except clause is for logging purposes.
        # Will be removed in future versions.
        except Exception:  # pylint: disable=broad-exception-caught
            logger.critical(
                "Found unexpected exception, please contact the developers", exc_info=True
            )
        try:
            if not conn.delete_server(name_or_id=server.name, wait=True, delete_ips=True):
                logger.warning("Server does not exist %s", server.name)
        except SDKException as exc:
            logger.error("Something wrong deleting the server %s, %s", server.name, exc)
        # TODO: 2024-04-23: The broad except clause is for logging purposes.
        # Will be removed in future versions.
        except Exception:  # pylint: disable=broad-exception-caught
            logger.critical(
                "Found unexpected exception, please contact the developers", exc_info=True
            )

    def _run_github_removal_script(
        self, conn: OpenstackConnection, server: Server, remove_token: str | None
    ) -> None:
        """Run Github runner removal script.

        Args:
            conn: The Openstack connection instance.
            server: The Openstack server instance.
            remove_token: The GitHub instance removal token.

        Raises:
            GithubRunnerRemoveError: Unable to remove runner from GitHub.
        """
        if not remove_token:
            return
        try:
            ssh_conn = OpenstackRunnerManager._get_ssh_connection(
                conn=conn, server_name=server.name
            )
        except _SSHError as exc:
            logger.error(
                "Unable to run GitHub removal script, server: %s, reason: %s",
                server.name,
                str(exc),
            )
            raise GithubRunnerRemoveError(
                f"Failed to remove runner {server.name} from Github."
            ) from exc

        try:
            result: Result = ssh_conn.run(
                f"{_CONFIG_SCRIPT_PATH} remove --token {remove_token}",
                warn=True,
            )
            if not result.ok:
                logger.warning(
                    (
                        "Unable to run removal script on instance %s, "
                        "exit code: %s, stdout: %s, stderr: %s"
                    ),
                    server.name,
                    result.return_code,
                    result.stdout,
                    result.stderr,
                )
            return
        # TODO: 2024-04-23: The broad except clause is for logging purposes.
        # Will be removed in future versions.
        except Exception:  # pylint: disable=broad-exception-caught
            logger.critical(
                "Found unexpected exception, please contact the developers", exc_info=True
            )

        logger.warning("Failed to run GitHub runner removal script %s", server.name)
        raise GithubRunnerRemoveError(f"Failed to remove runner {server.name} from Github.")

    def _clean_up_keys_files(
        self, conn: OpenstackConnection, exclude_instances: Iterable[str]
    ) -> None:
        """Delete all SSH key files except the specified instances.

        Args:
            conn: The Openstack connection instance.
            exclude_instances: The keys of these instance will not be deleted.
        """
        logger.info("Cleaning up SSH key files")
        exclude_filename = set(
            OpenstackRunnerManager._get_key_path(instance) for instance in exclude_instances
        )

        total = 0
        deleted = 0
        for path in _SSH_KEY_PATH.iterdir():
            # Find key file from this application.
            if (
                path.is_file()
                and path.name.startswith(self.instance_name)
                and path.name.endswith(".key")
            ):
                total += 1
                if path.name in exclude_filename:
                    continue

                keypair_name = path.name.split(".")[0]
                try:
                    conn.delete_keypair(keypair_name)
                except openstack.exceptions.SDKException:
                    logger.warning(
                        "Unable to delete OpenStack keypair associated with deleted key file %s ",
                        path.name,
                    )

                path.unlink()
                deleted += 1
        logger.info("Found %s key files, clean up %s key files", total, deleted)

    def _clean_up_openstack_keypairs(
        self, conn: OpenstackConnection, exclude_instances: Iterable[str]
    ) -> None:
        """Delete all OpenStack keypairs except the specified instances.

        Args:
            conn: The Openstack connection instance.
            exclude_instances: The keys of these instance will not be deleted.
        """
        logger.info("Cleaning up openstack keypairs")
        keypairs = conn.list_keypairs()
        for key in keypairs:
            # The `name` attribute is of resource.Body type.
            if key.name and str(key.name).startswith(self.instance_name):
                if str(key.name) in exclude_instances:
                    continue

                try:
                    conn.delete_keypair(key.name)
                except openstack.exceptions.SDKException:
                    logger.warning(
                        "Unable to delete OpenStack keypair associated with deleted key file %s ",
                        key.name,
                    )

    def _clean_up_runners(
        self, conn: OpenstackConnection, runner_by_health: RunnerByHealth, remove_token: str
    ) -> None:
        """Clean up offline or unhealthy runners.

        Args:
            conn: The openstack connection instance.
            runner_by_health: The runner status grouped by health.
            remove_token: The GitHub runner remove token.

        """
        github_info = self.get_github_runner_info()
        online_runners = [runner.runner_name for runner in github_info if runner.online]
        offline_runners = [runner.runner_name for runner in github_info if not runner.online]
        busy_runners = [runner.runner_name for runner in github_info if runner.busy]
        logger.info(
            "Found %s online and %s offline openstack runners, %s of the runners are busy",
            len(online_runners),
            len(offline_runners),
            len(busy_runners),
        )
        logger.debug("Online runner: %s", online_runners)
        logger.debug("Offline runner: %s", offline_runners)
        logger.debug("Busy runner: %s", busy_runners)

        healthy_runners_set = set(runner_by_health.healthy)
        busy_runners_set = set(busy_runners)
        busy_unhealthy_runners = set(runner_by_health.unhealthy).intersection(busy_runners_set)
        if busy_unhealthy_runners:
            logger.warning("Found unhealthy busy runners %s", busy_unhealthy_runners)

        # Clean up offline (SHUTOFF) runners or unhealthy (no connection/cloud-init script)
        # runners.
        # Possible for a healthy runner to be appear as offline for sometime as GitHub can be
        # slow to update the status.
        # For busy runners let GitHub decide whether the runner should be removed.
        instance_to_remove = tuple(
            runner
            for runner in (*runner_by_health.unhealthy, *offline_runners)
            if runner not in healthy_runners_set and runner not in busy_runners_set
        )
        logger.debug("Removing following runners with issues %s", instance_to_remove)
        self._remove_runners(
            conn=conn, instance_names=instance_to_remove, remove_token=remove_token
        )
        # Clean up orphan keys, e.g., If openstack instance is removed externally the key
        # would not be deleted.
        self._clean_up_keys_files(conn, runner_by_health.healthy)
        self._clean_up_openstack_keypairs(conn, runner_by_health.healthy)

    def _scale(
        self,
        quantity: int,
        conn: OpenstackConnection,
        runner_by_health: RunnerByHealth,
        remove_token: str,
    ) -> int:
        """Scale the number of runners.

        Args:
            quantity: The number of intended runners.
            conn: The openstack connection instance.
            runner_by_health: The runner status grouped by health.
            remove_token: The GitHub runner remove token.

        Returns:
            The change in number of runners.
        """
        # Get the number of OpenStack servers.
        # This is not calculated due to there might be removal failures.
        servers = self._get_openstack_instances(conn)
        delta = quantity - len(servers)
        registration_token = self._github.get_runner_registration_token(path=self._config.path)

        # Spawn new runners
        if delta > 0:
            logger.info("Creating %s OpenStack runners", delta)
            args = [
                OpenstackRunnerManager._CreateRunnerArgs(
                    app_name=self.app_name,
                    config=self._config,
                    cloud_config=self._cloud_config,
                    registration_token=registration_token,
                    unit_num=self.unit_num,
                )
                for _ in range(delta)
            ]
            with Pool(processes=min(delta, 10)) as pool:
                pool.map(
                    func=OpenstackRunnerManager._create_runner,
                    iterable=args,
                )

        elif delta < 0:
            logger.info("Removing %s OpenStack runners", delta)
            self._remove_runners(
                conn=conn,
                instance_names=runner_by_health.healthy,
                remove_token=remove_token,
                num_to_remove=abs(delta),
            )
        else:
            logger.info("No changes to number of runners needed")

        return delta

    def _issue_reconciliation_metrics(
        self,
        reconciliation_start_ts: float,
        reconciliation_end_ts: float,
    ) -> None:
        """Issue all reconciliation related metrics.

        This includes the metrics for the runners and the reconciliation metric itself.

        Args:
            reconciliation_start_ts: The timestamp of when reconciliation started.
            reconciliation_end_ts: The timestamp of when reconciliation ended.
        """
        with _create_connection(self._cloud_config) as conn:
            runner_states = self._get_openstack_runner_status(conn)

            metric_stats = self._issue_runner_metrics(conn)
            self._issue_reconciliation_metric(
                metric_stats=metric_stats,
                reconciliation_start_ts=reconciliation_start_ts,
                reconciliation_end_ts=reconciliation_end_ts,
                runner_states=runner_states,
            )

    def _issue_runner_metrics(self, conn: OpenstackConnection) -> IssuedMetricEventsStats:
        """Issue runner metrics.

        Args:
            conn: The connection object to access OpenStack cloud.

        Returns:
            The stats of issued metric events.
        """
        total_stats: IssuedMetricEventsStats = {}

        try:
            openstack_instances = self._get_openstack_instances(conn)
        except openstack.exceptions.SDKException:
            logger.exception(
                "Failed to get openstack instances to ignore when extracting metrics."
                " Will not issue runner metrics"
            )
            return total_stats

        logger.debug(
            "Found following openstack instances before extracting metrics: %s",
            openstack_instances,
        )
        # Don't extract metrics for instances which are still there, as it might be
        # the case that the metrics have not yet been pulled
        # (they get pulled right before server termination).
        instance_names = {instance.name for instance in openstack_instances}

        for extracted_metrics in runner_metrics.extract(
            metrics_storage_manager=metrics_storage,
            ignore_runners=instance_names,
        ):
            try:
                job_metrics = github_metrics.job(
                    github_client=self._github,
                    pre_job_metrics=extracted_metrics.pre_job,
                    runner_name=extracted_metrics.runner_name,
                )
            except GithubMetricsError:
                logger.exception("Failed to calculate job metrics")
                job_metrics = None

            issued_events = runner_metrics.issue_events(
                runner_metrics=extracted_metrics,
                job_metrics=job_metrics,
                flavor=self.app_name,
            )
            for event_type in issued_events:
                total_stats[event_type] = total_stats.get(event_type, 0) + 1
        return total_stats

    def _issue_reconciliation_metric(
        self,
        metric_stats: IssuedMetricEventsStats,
        reconciliation_start_ts: float,
        reconciliation_end_ts: float,
        runner_states: RunnerByHealth,
    ) -> None:
        """Issue reconciliation metric.

        Args:
            metric_stats: The stats of issued metric events.
            reconciliation_start_ts: The timestamp of when reconciliation started.
            reconciliation_end_ts: The timestamp of when reconciliation ended.
            runner_states: The states of the runners.
        """
        try:
            github_info = self.get_github_runner_info()
        except GithubApiError:
            logger.exception(
                "Failed to retrieve github info for reconciliation metric. "
                "Will not issue reconciliation metric."
            )
            return

        online_runners = [runner for runner in github_info if runner.online]
        offline_runner_names = {runner.runner_name for runner in github_info if not runner.online}
        active_runner_names = {runner.runner_name for runner in online_runners if runner.busy}
        healthy_runners = set(runner_states.healthy)

        active_count = len(active_runner_names)
        idle_online_count = len(online_runners) - active_count
        idle_offline_count = len((offline_runner_names & healthy_runners) - active_runner_names)

        try:
            metric_events.issue_event(
                event=metric_events.Reconciliation(
                    timestamp=time.time(),
                    flavor=self.app_name,
                    crashed_runners=metric_stats.get(metric_events.RunnerStart, 0)
                    - metric_stats.get(metric_events.RunnerStop, 0),
                    idle_runners=idle_online_count + idle_offline_count,
                    duration=reconciliation_end_ts - reconciliation_start_ts,
                )
            )
        except IssueMetricEventError:
            logger.exception("Failed to issue Reconciliation metric")

    def flush(self) -> int:
        """Flush Openstack servers.

        Returns:
            The number of runners flushed.
        """
        logger.info("Flushing OpenStack all runners")
        with _create_connection(self._cloud_config) as conn:
            runner_by_health = self._get_openstack_runner_status(conn)
            remove_token = self._github.get_runner_remove_token(path=self._config.path)
            runners_to_delete = (*runner_by_health.healthy, *runner_by_health.unhealthy)
            self._remove_runners(
                conn=conn,
                instance_names=runners_to_delete,
                remove_token=remove_token,
            )
            return len(runners_to_delete)
