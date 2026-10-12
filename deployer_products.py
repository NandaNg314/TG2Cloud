"""Immutable product definitions shared by the two PySide6 deployer entries."""

from __future__ import annotations

from dataclasses import dataclass

from payload_clouddrive2.app.version import VERSION as RELEASE_VERSION

SHARED_PAYLOAD_FILES = (
    "payload_clouddrive2/backup_retention.sh",
    "payload_clouddrive2/preserve_runtime_config.sh",
    "payload_clouddrive2/.dockerignore",
    "payload_clouddrive2/Dockerfile",
    "payload_clouddrive2/requirements.txt",
    "payload_clouddrive2/app/__init__.py",
    "payload_clouddrive2/app/version.py",
    "payload_clouddrive2/app/config.py",
    "payload_clouddrive2/app/main.py",
    "payload_clouddrive2/app/bot_commands.py",
    "payload_clouddrive2/app/interfaces.py",
    "payload_clouddrive2/app/states.py",
    "payload_clouddrive2/app/deployment_check.py",
    "payload_clouddrive2/app/backup_database.py",
    "payload_clouddrive2/app/naming.py",
    "payload_clouddrive2/app/db.py",
    "payload_clouddrive2/app/resources.py",
    "payload_clouddrive2/app/rclone_client.py",
    "payload_clouddrive2/app/healthcheck.py",
    "payload_clouddrive2/app/verify_destination.py",
)


@dataclass(frozen=True)
class ProductProfile:
    key: str
    display_name: str
    app_title: str
    app_version: str
    executable_name: str
    install_dir: str
    backup_dir: str
    webdav_url: str
    webdav_username: str
    webdav_target: str
    management_port: int
    bot_service: str
    bot_container: str
    storage_container: str
    docker_network: str
    requires_fuse: bool
    managed_toggle: bool
    payload_variant: str
    legacy_install_dirs: tuple[str, ...]
    legacy_containers: tuple[str, ...]
    legacy_networks: tuple[str, ...]
    required_payload: tuple[str, ...]

    @property
    def is_openlist(self) -> bool:
        return self.key == "openlist"


CLOUDDRIVE2_PRODUCT = ProductProfile(
    key="clouddrive2",
    display_name="CloudDrive2",
    app_title="TG2Cloud · CloudDrive2",
    app_version=RELEASE_VERSION,
    executable_name="TG2Cloud-CloudDrive2-Deployer",
    install_dir="/opt/tg2cloud-clouddrive2",
    backup_dir="/opt/tg2cloud-clouddrive2-backups",
    webdav_url="http://tg2cloud-clouddrive2:19798/dav",
    webdav_username="",
    webdav_target="",
    management_port=19798,
    bot_service="tg2cloud-clouddrive2-bot",
    bot_container="tg2cloud-clouddrive2-bot",
    storage_container="tg2cloud-clouddrive2",
    docker_network="tg2cloud-clouddrive2-net",
    requires_fuse=True,
    managed_toggle=True,
    payload_variant="payload_clouddrive2",
    legacy_install_dirs=("/opt/tg115",),
    legacy_containers=("tg115-bot", "tg115-clouddrive2"),
    legacy_networks=("tg115",),
    required_payload=SHARED_PAYLOAD_FILES
    + (
        "payload_clouddrive2/remote_install.sh",
        "payload_clouddrive2/manage.sh",
        "payload_clouddrive2/repair_clouddrive_network.sh",
        "payload_clouddrive2/docker-compose.yml",
    ),
)


OPENLIST_PRODUCT = ProductProfile(
    key="openlist",
    display_name="OpenList",
    app_title="TG2Cloud · OpenList",
    app_version=RELEASE_VERSION,
    executable_name="TG2Cloud-OpenList-Deployer",
    install_dir="/opt/tg2cloud-openlist",
    backup_dir="/opt/tg2cloud-openlist-backups",
    webdav_url="http://tg2cloud-openlist:5244/dav/",
    webdav_username="tg2cloud",
    webdav_target="",
    management_port=5244,
    bot_service="tg2cloud-openlist-bot",
    bot_container="tg2cloud-openlist-bot",
    storage_container="tg2cloud-openlist",
    docker_network="tg2cloud-openlist-net",
    requires_fuse=False,
    managed_toggle=False,
    payload_variant="payload_openlist",
    legacy_install_dirs=("/opt/tg115-openlist",),
    legacy_containers=("tg115-openlist-bot", "tg115-openlist"),
    legacy_networks=("tg115-openlist-net",),
    required_payload=SHARED_PAYLOAD_FILES
    + (
        "payload_openlist/remote_install.sh",
        "payload_openlist/manage.sh",
        "payload_openlist/openlist_admin.sh",
        "payload_openlist/preserve_webdav_config.sh",
        "payload_openlist/docker-compose.yml",
    ),
)


PRODUCTS = {
    CLOUDDRIVE2_PRODUCT.key: CLOUDDRIVE2_PRODUCT,
    OPENLIST_PRODUCT.key: OPENLIST_PRODUCT,
}
