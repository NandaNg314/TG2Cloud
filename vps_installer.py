"""Optional Linux install/upgrade wizard. Never used by either Windows entry."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from deployer_products import PRODUCTS, ProductProfile
from domain_proxy import (
    DomainProxyManager,
    dns_matches_environment,
    validate_domain,
    validate_email,
)
from scripts.vps_runtime import LocalSession, SafeLog, edition_lock
from vps_resources import (
    assess_storage_choice,
    build_probe_command,
    parse_probe_output,
    validate_install_dir,
)

API = "https://api.github.com/repos/LuoPoJunZi/TG2Cloud"
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
MAX_ARCHIVE = 100 * 1024 * 1024


def validate_public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {
        "api.github.com", "codeload.github.com", "raw.githubusercontent.com",
    } or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError("下载地址必须是公开 GitHub HTTPS 地址")


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        validate_public_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


def open_public(url: str, *, timeout: float):
    validate_public_url(url)
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "TG2Cloud-VPS-installer",
    })
    return urllib.request.build_opener(PublicRedirect()).open(request, timeout=timeout)


def version_tuple(version: str) -> tuple[int, int, int]:
    match = TAG.fullmatch("v" + version)
    if not match:
        raise ValueError("版本号不是正式 X.Y.Z 格式")
    return tuple(map(int, match.groups()))


def source_version(root: Path) -> str:
    try:
        path = root / "app/version.py"
        variable = "VERSION"
        # Published older payloads predate the single version module. Only
        # accept their literal __version__; full official-source validation
        # remains mandatory before constructing any upgrade plan.
        if not path.exists() and not path.is_symlink():
            path = root / "app/__init__.py"
            variable = "__version__"
        if path.is_symlink():
            raise ValueError("版本文件经过链接")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values = [node.value.value for node in tree.body
                  if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                  and any(isinstance(name, ast.Name) and name.id == variable for name in node.targets)]
        if len(values) != 1 or not isinstance(values[0], str):
            raise ValueError("版本源缺失")
        version_tuple(values[0])
        return values[0]
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValueError("无法安全读取程序版本；不会猜测版本或覆盖实例") from exc


@dataclass(frozen=True)
class Release:
    tag: str
    commit: str

    @property
    def version(self) -> str:
        return self.tag[1:]


class ReleaseClient:
    """Public HTTPS only: no credentials, main fallback, or executable assets."""

    def json(self, url: str) -> dict:
        try:
            with open_public(url, timeout=25) as response:
                body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("GitHub 响应过大")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("GitHub 响应格式不正确")  # noqa: TRY004 - invalid wire format
            return value
        except (OSError, ValueError) as exc:
            raise RuntimeError("无法读取 GitHub 正式 Release；请检查网络/限流后重试，不会改用 main") from exc

    def resolve(self, tag: str | None = None) -> Release:
        if tag is not None and not TAG.fullmatch(tag):
            raise ValueError("只允许正式 vX.Y.Z Tag")
        release = self.json(API + ("/releases/tags/" + tag if tag else "/releases/latest"))
        name = release.get("tag_name", "")
        if release.get("draft") is not False or release.get("prerelease") is not False or not TAG.fullmatch(name):
            raise ValueError("拒绝草稿、RC 或非正式版本")
        if tag and name != tag:
            raise ValueError("Release Tag 不一致")
        item = self.json(API + "/git/ref/tags/" + name).get("object", {})
        for _ in range(5):
            sha = item.get("sha", "")
            if not SHA.fullmatch(sha):
                raise ValueError("Release 提交标识无效")
            if item.get("type") == "commit":
                return Release(name, sha)
            if item.get("type") != "tag":
                break
            item = self.json(API + "/git/tags/" + sha).get("object", {})
        raise ValueError("无法将 Release 固定到唯一 commit")

    def download(self, release: Release, destination: Path) -> Path:
        archive = destination / (release.commit + ".tar.gz")
        try:
            url = "https://codeload.github.com/LuoPoJunZi/TG2Cloud/tar.gz/" + release.commit
            with open_public(url, timeout=60) as response, archive.open("xb") as writer:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise ValueError("源码归档过大")
                    writer.write(chunk)
            root = destination / ("source-" + release.commit)
            extract_source(archive, root)
            if source_version(root / "payload_clouddrive2") != release.version:
                raise ValueError("Tag 与源码版本不一致")
            return root
        except (OSError, tarfile.TarError) as exc:
            raise RuntimeError("稳定版本源码下载/解包失败；未修改运行实例") from exc


def extract_source(archive: Path, root: Path) -> None:
    """Never extract links, devices, traversal, duplicates, or an archive bomb."""
    root.mkdir(mode=0o700)
    seen: set[str] = set()
    top = None
    total = 0
    with tarfile.open(archive, "r:gz") as source:
        for number, member in enumerate(source):
            parts = PurePosixPath(member.name).parts
            if (number >= 20000 or not parts or member.name.startswith("/")
                    or ".." in parts or "\\" in member.name or "\x00" in member.name
                    or not (member.isfile() or member.isdir())):
                raise ValueError("源码归档包含不安全路径或文件类型")
            if top is None:
                top = parts[0]
            if parts[0] != top:
                raise ValueError("源码归档包含多个顶层目录")
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError("源码归档顶层不是目录")
                continue
            name = "/".join(parts[1:])
            if name in seen:
                raise ValueError("源码归档包含重复路径")
            seen.add(name)
            total += member.size
            if member.size < 0 or member.size > 32 * 1024 * 1024 or total > 250 * 1024 * 1024:
                raise ValueError("源码归档解压大小超限")
            target = root.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with source.extractfile(member) as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                target.chmod(0o600)


def payload_sources(root: Path, product: ProductProfile, *, strict: bool = True) -> dict[str, Path]:
    result = {}
    for relative in product.required_payload:
        source = root / relative
        if not source.is_file() or source.is_symlink():
            if strict:
                raise ValueError("稳定版本缺少必要部署资源：" + relative)
            continue
        result[relative.split("/", 1)[1]] = source
    return result


def build_payload(root: Path, product: ProductProfile, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    for relative, source in payload_sources(root, product).items():
        target = destination / relative
        # Only manifest-selected public source enters this Docker context.
        # COPY preserves its modes, so UID 10001 needs readable files and
        # traversable subdirectories. The enclosing private directory stays
        # 0700; candidate credentials remain outside the context at 0600.
        parent = destination
        for part in Path(relative).parts[:-1]:
            parent = parent / part
            parent.mkdir(mode=0o755, exist_ok=True)
            parent.chmod(0o755)
        shutil.copyfile(source, target)
        target.chmod(0o644)
    for name in ("LICENSE", "NOTICE", "README.md"):
        if (root / name).is_file():
            shutil.copyfile(root / name, destination / name)
            (destination / name).chmod(0o644)


def regular_private(path: Path) -> bytes:
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ValueError("必要文件缺失或经过链接：" + path.name)
    info = path.stat()
    if info.st_size > 128 * 1024:
        raise ValueError("配置文件异常过大")
    if os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o077):
        raise ValueError("配置必须归 root 所有，且不允许组/其他用户读取")
    return path.read_bytes()


def parse_config(raw: bytes) -> dict[str, str]:
    result = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise ValueError("配置存在无效/重复项目；不会执行配置内容")
        result[key] = value
    if result.get("TG2CLOUD_REDEPLOY_APPLY_CONFIG") not in (None, "true", "false"):
        raise ValueError("配置覆盖标记无效")
    return result


def checked_directory(value: str) -> Path:
    validate_install_dir(value)
    path = Path(value)
    if path.resolve() != path or path.is_symlink():
        raise ValueError("安装目录经过链接；不会操作")
    return path


def check_product_directory(product: ProductProfile, directory: Path) -> None:
    protected = [Path("/opt/tg2cloud-proxy"), Path("/opt/tg2cloud-cli-locks")]
    for profile in PRODUCTS.values():
        protected += [Path(profile.backup_dir), *(Path(name) for name in profile.legacy_install_dirs)]
        if profile.key != product.key:
            protected.append(Path(profile.install_dir))
    if any(directory == root or directory.is_relative_to(root) or root.is_relative_to(directory) for root in protected):
        raise ValueError("安装目录与其他 Edition、代理、锁、备份或 TG115 路径重叠")


def inspect_container(session: LocalSession, name: str) -> dict:
    template = ('{"image":{{json .Config.Image}},"running":{{json .State.Running}},'
                '"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}')
    code, output = session.run(
        "docker container inspect --format " + shlex.quote(template) + " " + shlex.quote(name), timeout=15,
    )
    if code:
        raise RuntimeError("容器检查失败；不会把不可读容器当作未安装")
    return json.loads(output)


def docker_inventory(session: LocalSession) -> set[str]:
    code, _ = session.run("command -v docker >/dev/null 2>&1", timeout=10)
    if code:
        return set()
    code, output = session.run("docker ps -a --format '{{.Names}}'", timeout=15)
    if code:
        raise RuntimeError("Docker 已安装但不可用；不会自动重启或升级 Docker")
    return set(output.splitlines())


@dataclass
class Instance:
    product: ProductProfile
    directory: Path
    present: bool
    version: str = ""
    running: bool = False
    gateway_image: str = ""
    config: dict[str, str] = field(default_factory=dict, repr=False)
    raw_config: bytes = field(default=b"", repr=False)


def discover(
    product: ProductProfile, inventory: set[str], session: LocalSession, log: SafeLog,
    *, install_dir: str | None = None,
) -> Instance:
    default = checked_directory(install_dir or product.install_dir)
    check_product_directory(product, default)
    has_bot = product.bot_container in inventory
    has_gateway = product.storage_container in inventory
    if not has_bot and not has_gateway:
        if any(name in inventory for name in product.legacy_containers) or any(
            Path(name).exists() for name in product.legacy_install_dirs
        ):
            raise ValueError("发现旧 TG115；请按迁移文档人工迁移，不自动覆盖")
        if default.exists() and any(default.iterdir()):
            raise ValueError("安装目录有残留但缺少容器；停止，不能当作首次安装覆盖")
        return Instance(product, default, False)
    if not has_bot or not has_gateway:
        raise ValueError("仅检测到部分容器；请先核对实例，不自动重装")
    bot = inspect_container(session, product.bot_container)
    gateway = inspect_container(session, product.storage_container)
    labels = bot.get("labels") or {}
    if not labels.get("com.docker.compose.project"):
        raise ValueError("容器缺少 Compose 项目身份")
    directory = checked_directory(labels.get("com.docker.compose.project.working_dir", ""))
    check_product_directory(product, directory)
    if install_dir and directory != default:
        raise ValueError("指定目录与容器实际目录不一致")
    if labels.get("com.docker.compose.service") != product.bot_service:
        raise ValueError("同名 Bot 容器不属于预期 Compose 服务")
    gateway_labels = gateway.get("labels") or {}
    if (gateway_labels.get("com.docker.compose.project.working_dir") != str(directory)
            or gateway_labels.get("com.docker.compose.project") != labels.get("com.docker.compose.project")
            or gateway_labels.get("com.docker.compose.service") != ("openlist" if product.is_openlist else "clouddrive2")):
        raise ValueError("同名网关容器不属于此实例")
    mounts = {item["Destination"]: item for item in bot.get("mounts", [])}
    for destination, relative in (("/data", "data"), ("/config", "config"), ("/downloads", "downloads"), ("/logs", "logs")):
        mount = mounts.get(destination, {})
        if mount.get("Type") != "bind" or mount.get("Source") != str(directory / relative):
            raise ValueError("Bot 持久化挂载与安装目录不一致")
    gateway_mounts = {item["Destination"]: item for item in gateway.get("mounts", [])}
    expected_mounts = (("/opt/openlist/data", "openlist/data"),) if product.is_openlist else (
        ("/Config", "clouddrive/config"), ("/CloudNAS", "clouddrive/mounts"),
    )
    for destination, relative in expected_mounts:
        mount = gateway_mounts.get(destination, {})
        child = directory / relative
        if (mount.get("Type") != "bind" or mount.get("Source") != str(child)
                or not child.is_dir() or child.resolve() != child):
            raise ValueError("云网关持久化挂载与安装目录不一致")
    raw = regular_private(directory / ".env")
    if not product.is_openlist and b"\r" in raw:
        raise ValueError("CloudDrive2 .env 含 CRLF/回车；现有安装器会规范化它，为保留原始字节已停止，请人工核对")
    config = parse_config(raw)
    log.register(config)
    backend = config.get("TG2CLOUD_STORAGE_BACKEND", config.get("TG115_STORAGE_BACKEND"))
    if backend != product.key or (not product.is_openlist and config.get("DEPLOY_CLOUDDRIVE2", "true") != "true"):
        raise ValueError("实例不是此 Edition 的受管网关部署；不会覆盖外部 WebDAV 配置")
    for name in ("app", "data", "config", "downloads", "logs"):
        child = directory / name
        if not child.is_dir() or child.resolve() != child:
            raise ValueError("持久化/程序目录缺失或经过链接")
    for path in (directory / "app").rglob("*.py"):
        if path.resolve() != path or not path.is_file():
            raise ValueError("程序文件经过链接")
    version = source_version(directory)
    code, output = session.run(
        "docker compose -f " + shlex.quote(str(directory / "docker-compose.yml")) + " config --format json", timeout=20,
    )
    if code:
        raise ValueError("现有 Compose 配置无法解析；未显示配置内容")
    services = json.loads(output)["services"]
    if (services[product.bot_service].get("container_name") != product.bot_container
            or services["openlist" if product.is_openlist else "clouddrive2"].get("container_name") != product.storage_container):
        raise ValueError("Compose 与容器身份不一致")
    if bot.get("running"):
        script = (
            "from app import __version__; from app.deployment_check import code_fingerprint; "
            "import json; print(json.dumps({'version':__version__,'fingerprint':code_fingerprint()}))"
        )
        code, output = session.run("docker exec " + product.bot_container + " python -c " + shlex.quote(script), timeout=20)
        if code:
            raise ValueError("无法核对实际运行版本/代码；停止升级")
        actual = json.loads(output)
        fingerprint = "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  app/{path.name}\n"
            for path in sorted((directory / "app").glob("*.py"))
        )
        if actual.get("version") != version or actual.get("fingerprint") != hashlib.sha256(fingerprint.encode()).hexdigest():
            raise ValueError("运行代码与磁盘程序不一致；不会猜测或自动覆盖")
    return Instance(product, directory, True, version, bool(bot.get("running")), gateway["image"], config, raw)


def assert_unmodified(instance: Instance, old_source: Path) -> None:
    expected = payload_sources(old_source, instance.product, strict=False)
    version_file = "app/version.py" if "app/version.py" in expected else "app/__init__.py"
    if not {"app/main.py", version_file, "Dockerfile", "docker-compose.yml", "manage.sh"} <= expected.keys():
        raise ValueError("旧版缺少可验证部署清单；请使用部署器人工核对")
    for relative, source in expected.items():
        local = instance.directory / relative
        if not local.is_file() or local.resolve() != local:
            raise ValueError("现有程序缺失或经过链接：" + relative)
        # Windows EXE payloads may carry CRLF. This is not a custom code edit.
        if local.read_bytes().replace(b"\r\n", b"\n") != source.read_bytes().replace(b"\r\n", b"\n"):
            raise ValueError("现有程序与已发布版本不一致，需人工核对：" + relative)
    expected_apps = {name for name in expected if name.startswith("app/")}
    actual_apps = {path.relative_to(instance.directory).as_posix() for path in (instance.directory / "app").rglob("*.py")}
    if expected_apps != actual_apps:
        raise ValueError("现有 Bot 包含额外/缺失源码；停止自动覆盖")


def action_for(instance: Instance, release: Release) -> str:
    if not instance.present:
        return "install"
    current, target = version_tuple(instance.version), version_tuple(release.version)
    if current > target:
        return "newer"
    if current == target:
        return "current"
    if current[0] != target[0]:
        raise ValueError("跨主版本升级需要人工核对迁移说明")
    return "upgrade"


def gateway_image(source: Path, product: ProductProfile) -> str:
    text = (source / product.payload_variant / "docker-compose.yml").read_text(encoding="utf-8")
    images = re.findall(r"(?m)^    image: ([^\r\n]+)$", text)
    if len(images) != 1 or not re.fullmatch(r"[A-Za-z0-9_./:-]+@sha256:[0-9a-f]{64}", images[0]):
        raise ValueError("网关镜像不是可识别的固定 digest")
    return images[0]


def check_foreign_gateway(product: ProductProfile, session: LocalSession) -> None:
    if product.is_openlist:
        return
    code, output = session.run("docker ps -a --format '{{.Names}} {{.Image}}'", timeout=15)
    if code:
        available, _ = session.run("command -v docker >/dev/null 2>&1", timeout=10)
        if not available:
            raise ValueError("Docker 容器列表无法检查；不会跳过外部实例冲突检查")
        return  # A fresh host may not have Docker yet.
    for line in output.splitlines():
        name, _, image = line.partition(" ")
        matches = "clouddrive2" in name.lower() or re.search(r"(^|/)clouddrive2([:@]|$)", image.lower())
        if matches and name not in {product.storage_container, product.bot_container}:
            raise ValueError("存在其他 CloudDrive2 容器；请人工处理冲突，不自动接管")


def encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def new_config(product: ProductProfile, values: dict[str, str]) -> dict[str, str]:
    result = {
        "TELEGRAM_API_ID": values["api_id"], "ALLOWED_USER_ID": values["user_id"],
        "TELEGRAM_API_HASH_B64": encoded(values["api_hash"]), "BOT_TOKEN_B64": encoded(values["bot_token"]),
        "LOCAL_TEMP_BUDGET_GB": values["budget"], "MIN_FREE_DISK_GB": values["reserve"],
        "CONTROL_INTERVAL_SECONDS": "3", "CPU_TARGET_LOW_PERCENT": "60", "CPU_TARGET_HIGH_PERCENT": "80",
        "CPU_PRESSURE_PERCENT": "90", "MEMORY_SOFT_MIN_MB": "1024", "MEMORY_HARD_MIN_MB": "512",
        "RAMP_UP_STEP": "1", "RAMP_DOWN_FACTOR": "0.5", "MAX_RETRIES": "3",
        "REMOTE_HEALTH_INTERVAL_SECONDS": "30", "TZ": values["timezone"],
        "DEPLOY_CLOUDDRIVE2": "false" if product.is_openlist else "true",
        "TG2CLOUD_REDEPLOY_APPLY_CONFIG": "false",
    }
    for prefix in ("TG2CLOUD", "TG115"):
        result[prefix + "_STORAGE_BACKEND"] = product.key
        result[prefix + "_DESTINATION_LABEL"] = product.display_name
        result[prefix + "_RCLONE_REMOTE_NAME"] = "openlist" if product.is_openlist else "cd2"
    for key, value in (("URL", product.webdav_url), ("USERNAME", values["webdav_user"]), ("PASSWORD", values["webdav_password"])):
        result["WEBDAV_" + key + "_B64"] = encoded(value)
        result["CD2_WEBDAV_" + key + "_B64"] = encoded(value)
    result["WEBDAV_TARGET_PATH_B64"] = result["CD2_TARGET_PATH_B64"] = encoded(values["target"])
    if product.is_openlist:
        result["OPENLIST_ADMIN_PASSWORD"] = values["admin_password"]
        result["TG2CLOUD_PRESERVE_WEBDAV"] = result["TG115_PRESERVE_WEBDAV"] = "false"
    return result


def positive(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,14}", value):
        raise ValueError("请输入大于零的整数")
    return value


def size_gb(value: str) -> str:
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 100000:
        raise ValueError("请输入有限的正数 GB")
    return value


def plain(value: str) -> str:
    if not value or len(value) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("输入为空、过长或含控制字符")
    return value


class Terminal:
    def __init__(self) -> None:
        # A terminal is not seekable: buffered r+ (BufferedRandom) fails before
        # the first prompt. Separate streams still use only the controlling TTY.
        with ExitStack() as streams:
            self.tty = streams.enter_context(open("/dev/tty", "r", encoding="utf-8", buffering=1))
            self.output = streams.enter_context(open("/dev/tty", "w", encoding="utf-8", buffering=1))
            if not self.tty.isatty() or not self.output.isatty():
                raise RuntimeError("需要交互终端；不从管道/历史记录读取密码")
            self._streams = streams.pop_all()

    def close(self) -> None:
        self._streams.close()

    def ask(self, prompt: str, *, default: str = "", secret: bool = False, validate=plain) -> str:
        import termios

        while True:
            previous = None
            try:
                if secret:
                    previous = termios.tcgetattr(self.tty.fileno())
                    hidden = previous.copy()
                    hidden[3] &= ~termios.ECHO
                    termios.tcsetattr(self.tty.fileno(), termios.TCSANOW, hidden)
                self.output.write(prompt + (f" [{default}]" if default and not secret else "") + ": ")
                self.output.flush()
                line = self.tty.readline()
            finally:
                if previous is not None:
                    termios.tcsetattr(self.tty.fileno(), termios.TCSANOW, previous)
                    self.output.write("\n")
            if not line:
                raise EOFError("输入已中断")
            value = line.rstrip("\r\n") or default
            try:
                return validate(value)
            except (ValueError, ZoneInfoNotFoundError):
                self.output.write("输入格式不正确，请重新填写（不显示输入值）。\n")

    def confirm(self, prompt: str) -> bool:
        return self.ask(prompt + " [y/N]", default="n", validate=lambda text: text.lower()) == "y"

    def reveal(self, label: str, value: str) -> None:
        if self.confirm("是否仅在当前终端显示" + label + "？请确保没有旁观/录屏"):
            self.output.write(label + ": " + value + "\n")
            self.output.flush()


def show_credentials(ui: Terminal, product: ProductProfile, config: dict[str, str], log: SafeLog) -> None:
    """Read saved setup values only; never query/reset a gateway password."""
    log(product.display_name + "：逐项确认后仅向当前私有终端显示已保存凭据；默认不显示。")
    for key, label in (("USERNAME", "WebDAV 用户名"), ("PASSWORD", "WebDAV 密码")):
        # Older CloudDrive2 installations may only have the compatibility keys.
        encoded_value = config.get("WEBDAV_" + key + "_B64")
        if encoded_value is None:
            encoded_value = config.get("CD2_WEBDAV_" + key + "_B64", "")
        try:
            value = plain(base64.b64decode(encoded_value, validate=True).decode("utf-8"))
        except (ValueError, UnicodeError):
            log(label + "未保存或格式无法安全显示；请在私有终端核对，不会重置。")
            continue
        ui.reveal(label, value)
    if product.is_openlist:
        log("OpenList 仅保存初始化管理员密码；若已在管理页更改，它不代表当前密码。")
        try:
            value = plain(config.get("OPENLIST_ADMIN_PASSWORD", ""))
        except ValueError:
            value = ""
        if value:
            ui.reveal("OpenList 初始化管理员密码（可能已更改）", value)
        else:
            log("未保存可显示的 OpenList 初始化管理员密码；不会重置。")


def collect_config(ui: Terminal, product: ProductProfile) -> dict[str, str]:
    def api_hash(value: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
            raise ValueError("API Hash 格式不正确")
        return value

    def token(value: str) -> str:
        if not re.fullmatch(r"[0-9]{6,12}:[A-Za-z0-9_-]{20,}", value):
            raise ValueError("Bot Token 格式不正确")
        return value

    def target(value: str) -> str:
        value = value.strip().strip("/")
        if value:
            plain(value)
        if "\\" in value or any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("目标路径不能包含父目录或反斜杠")
        return value

    def timezone(value: str) -> str:
        ZoneInfo(value)
        return value

    values = {
        "api_id": ui.ask("Telegram API ID", validate=positive),
        "api_hash": ui.ask("Telegram API Hash（隐藏输入）", secret=True, validate=api_hash),
        "bot_token": ui.ask("Bot Token（隐藏输入）", secret=True, validate=token),
        "user_id": ui.ask("允许使用 Bot 的 Telegram 用户 ID", validate=positive),
        "webdav_user": ui.ask("专用 WebDAV 用户名", default=product.webdav_username or "tg2cloud"),
        "webdav_password": ui.ask("WebDAV 密码（留空安全生成）", secret=True, default=secrets.token_urlsafe(21)),
        "target": ui.ask("WebDAV 根目录下的子目录（根目录已选中目标时留空）", validate=target),
        "budget": ui.ask("本地任务预算 GB", default="20", validate=size_gb),
        "reserve": ui.ask("磁盘最少保留 GB", default="8", validate=size_gb),
        "timezone": ui.ask("时区", default="Asia/Shanghai", validate=timezone),
        "admin_password": secrets.token_urlsafe(21),
    }
    if float(values["reserve"]) < 8:
        raise ValueError("首版首次安装的磁盘保留空间不能低于 8GB")
    return new_config(product, values)


@dataclass
class Plan:
    instance: Instance
    action: str
    manager: DomainProxyManager
    config: dict[str, str] = field(default_factory=dict, repr=False)
    domain: str = ""
    email: str = ""
    configure_https: bool = False


def validate_host() -> None:
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise ValueError("请在 Linux VPS 以 root 运行；本入口不是 Windows EXE 的替代品")
    if platform.machine() not in {"x86_64", "amd64"}:
        raise ValueError("首版仅支持 Debian/Ubuntu x86_64 Docker VPS")
    release = Path("/etc/os-release").read_text(encoding="utf-8")
    if not re.search(r'(?m)^ID=(?:"?(?:debian|ubuntu)"?)$', release):
        raise ValueError("首版仅支持 Debian/Ubuntu；不猜测其他发行版的安装方式")
    if shutil.which("bash") is None:
        raise ValueError("缺少 Bash")


def resource_check(instance: Instance, config: dict[str, str], session: LocalSession, log: SafeLog) -> None:
    code, output = session.run(
        build_probe_command(str(instance.directory), instance.product.backup_dir), timeout=45,
    )
    if code:
        raise ValueError("资源预检未完整执行")
    resources = parse_probe_output(output)
    assessment = assess_storage_choice(
        resources, budget_gb=float(size_gb(config.get("LOCAL_TEMP_BUDGET_GB", "20"))),
        reserve_gb=float(size_gb(config.get("MIN_FREE_DISK_GB", "8"))),
        managed_clouddrive=not instance.product.is_openlist, managed_storage=True,
        requires_fuse=instance.product.requires_fuse,
        minimum_memory_mb=900 if instance.product.is_openlist else 1800,
    )
    log(assessment.reason)
    if not assessment.safe:
        raise ValueError("资源不足：" + assessment.reason + "；不会自动降低预算或修改已有配置")


def make_plan(
    instance: Instance, release: Release, source: Path, old_source: Path | None,
    session: LocalSession, log: SafeLog, ui: Terminal | None, *, configure_https: bool = False,
) -> Plan:
    action = action_for(instance, release)
    if action in {"install", "upgrade"}:
        payload_sources(source, instance.product)
    if instance.present:
        if old_source is None:
            raise ValueError("缺少当前版本的官方源码基线，无法安全升级")
        assert_unmodified(instance, old_source)
        if action == "upgrade" and gateway_image(source, instance.product) != instance.gateway_image:
            raise ValueError("此升级涉及云网关镜像变化；首版 CLI 不做网关数据库迁移，请使用部署器并人工验收")
    elif any(name in docker_inventory(session) for name in instance.product.legacy_containers):
        raise ValueError("发现旧 TG115 容器，停止首次安装")
    if action in {"upgrade", "install"}:
        # The stable repair helper can adopt foreign CD2 containers; refuse them.
        check_foreign_gateway(instance.product, session)
    manager = DomainProxyManager(session, instance.product, {"vps_host": "127.0.0.1"}, log)
    manager.prepare()
    state = manager.read_state()
    report = manager.status(state)
    log(instance.product.display_name + " HTTPS：" + report.message)
    plan = Plan(instance, action, manager, dict(instance.config), report.domain)
    needs_https = action == "install" or configure_https
    if action == "upgrade" and report.state != "healthy" and not configure_https:
        raise ValueError("已有 HTTPS 未通过；先用 --configure-https 显式修复，不在升级中隐式变更域名")
    if configure_https and action == "newer":
        raise ValueError("当前实例新于 latest；请使用匹配版本的部署器维护 HTTPS")
    if needs_https and report.state != "healthy":
        plan.configure_https = True
        if ui is not None:
            # Reuse an existing route, never offer an implicit domain switch.
            plan.domain = report.domain or ui.ask("管理页域名（A/AAAA 直指本机；Cloudflare 仅 DNS）", validate=validate_domain)
            old_route = state["domains"].get(instance.product.key, {})
            plan.email = old_route.get("email", "") or ui.ask("Let's Encrypt 邮箱（可留空）", validate=validate_email)
            if any(item["domain"] == plan.domain for key, item in state["domains"].items() if key != instance.product.key):
                raise ValueError("该域名已由另一个 Edition 使用")
            if instance.present:
                detection = manager.detect(plan.domain)
                if detection.state != "detected":
                    raise ValueError(detection.message)
            else:
                environment = manager.probe(plan.domain)
                if not environment.ports_safe or not dns_matches_environment(environment, "127.0.0.1"):
                    raise ValueError("首次安装 HTTPS 前置检查未通过：检查 DNS 直指 VPS，以及 80/443 端口是否可安全使用")
    if action == "install" and ui is not None:
        plan.config = collect_config(ui, instance.product)
        log.register(plan.config)
    if action in {"install", "upgrade"} and (ui is not None or instance.present):
        resource_check(instance, plan.config, session, log)
    if action == "upgrade":
        code, _ = session.run("docker compose version >/dev/null 2>&1", timeout=15)
        if code:
            raise ValueError("升级要求现有 Docker Compose 可用；不在升级中自动安装依赖")
    return plan


def preview(plan: Plan, release: Release, log: SafeLog) -> None:
    instance = plan.instance
    labels = {"install": "首次安装", "upgrade": "保留配置升级", "current": "已是最新，不重建", "newer": "高于 latest，不降级"}
    log(f"{instance.product.display_name}：{labels[plan.action]}；{instance.version or '未安装'} → {release.version}")
    log("安装目录：" + str(instance.directory))
    if plan.action == "upgrade":
        log("保留 .env、Session、SQLite、downloads 和云网关数据；所选 Bot 将短暂停止，执行现有备份/回退流程。")
        log("复用安装器的 APT 基础依赖检查，可能更新相关系统软件包；不主动升级 Docker 引擎。")
    if plan.action == "install":
        log("将通过现有安装器安装 APT/Docker 依赖及固定镜像；配置只保存在本机私有文件。")
        if plan.config:
            log("任务预算/保留空间 GB：" + plan.config["LOCAL_TEMP_BUDGET_GB"] + " / " + plan.config["MIN_FREE_DISK_GB"])
    log("HTTPS：" + ("显式配置 " + (plan.domain or "尚需收集域名") if plan.configure_https else "不修改现有路由/证书"))
    log("不切换另一 Edition 的域名，不迁移 TG115；不会重启未选中的 Bot。")


def record_release(directory: Path, release: Release) -> None:
    """Write identity only after successful installation, never into rollback payload."""
    target = directory / "tg2cloud-release.json"
    if target.is_symlink() or target.resolve() != target:
        raise ValueError("版本记录文件经过链接")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".release-", delete=False) as writer:
        staged = Path(writer.name)
        try:
            os.chmod(staged, 0o600)
            json.dump({"tag": release.tag, "commit": release.commit}, writer)
            writer.write("\n")
            writer.flush()
            os.fsync(writer.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    try:
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def apply_base(plan: Plan, release: Release, source: Path, session: LocalSession, log: SafeLog) -> None:
    instance = plan.instance
    if plan.action not in {"install", "upgrade"}:
        return
    with tempfile.TemporaryDirectory(prefix="tg2cloud-cli-payload-") as private:
        root = Path(private)
        payload = root / "payload"
        build_payload(source, instance.product, payload)
        candidate = root / "config.env"
        content = ("TG2CLOUD_REDEPLOY_APPLY_CONFIG=false\n" if instance.present else
                   "".join(key + "=" + value + "\n" for key, value in plan.config.items()))
        with candidate.open("x", encoding="utf-8") as writer:
            candidate.chmod(0o600)
            writer.write(content)
        command = ("INSTALL_DIR=" + shlex.quote(str(instance.directory)) + " bash "
                   + shlex.quote(str(payload / "remote_install.sh")) + " "
                   + shlex.quote(str(payload)) + " " + shlex.quote(str(candidate)))
        code, output = session.run(command, stream=log, timeout=2400)
        if code or "TG2CLOUD_RESULT=SUCCESS" not in output.splitlines() or "TG2CLOUD_BOT_HEALTH=healthy" not in output.splitlines():
            raise RuntimeError("基础安装/升级失败；请核对现有安装器的回退结果和备份，不假定已恢复")
    if instance.present and regular_private(instance.directory / ".env") != instance.raw_config:
        raise RuntimeError("升级后 .env 字节发生变化，停止后续操作；请核对备份")
    record_release(instance.directory, release)


def verify_destination(plan: Plan, session: LocalSession, log: SafeLog, ui: Terminal) -> bool:
    if not ui.confirm("运行 WebDAV 写入验收？会创建并清理随机测试文件；请先完成云挂载/专用 WebDAV 用户配置"):
        log("TG2CLOUD_CLI_WEBDAV=NOT_RUN；未把基础健康状态当作转存验收通过")
        return False
    directory = shlex.quote(str(plan.instance.directory))
    code, output = session.run("cd " + directory + " && INSTALL_DIR=" + directory + " bash ./manage.sh verify", stream=log, timeout=600)
    if code or "TG2CLOUD_DESTINATION=OK" not in output.splitlines():
        raise RuntimeError("WebDAV 验收未通过；不自动重复验收或修改凭据，请查看脱敏日志")
    log("TG2CLOUD_CLI_WEBDAV=OK；仍请在云存储官方客户端确认最终文件")
    return True


def execute_plan(
    plan: Plan, release: Release, source: Path, old_source: Path | None,
    session: LocalSession, log: SafeLog, ui: Terminal, *, verify: bool = False,
) -> None:
    instance = plan.instance
    with edition_lock(Path("/opt/tg2cloud-cli-locks") / (instance.product.key + ".lock")):
        if instance.present:
            current = discover(instance.product, docker_inventory(session), session, log, install_dir=str(instance.directory))
            if (current.raw_config != instance.raw_config or current.version != instance.version
                    or current.gateway_image != instance.gateway_image):
                raise ValueError("预览之后实例配置/版本已变更；停止，重新预检")
            assert_unmodified(current, old_source)
        else:
            current = discover(instance.product, docker_inventory(session), session, log, install_dir=str(instance.directory))
            if current.present:
                raise ValueError("预览之后出现实例；停止，不覆盖")
        if plan.action in {"install", "upgrade"}:
            check_foreign_gateway(instance.product, session)
        apply_base(plan, release, source, session, log)
        if plan.configure_https:
            plan.manager.configure(plan.domain, plan.email)
        report = plan.manager.status()
        log(report.message)
        if report.state != "healthy":
            raise RuntimeError("基础服务可能已安装，但 HTTPS 尚未通过；用 --configure-https 显式续做，不要重装/删除数据")
        log("TG2CLOUD_CLI_BASE_HTTPS=OK；公网访问仍需外部网络检查")
        if plan.action == "install" or plan.configure_https:
            log("请在 https://" + report.domain + " 配置自己的云存储，再建立专用 WebDAV 用户。")
            show_credentials(ui, instance.product, plan.config, log)
        if verify or plan.action == "install":
            verify_destination(plan, session, log, ui)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="TG2Cloud 可选 VPS 向导：稳定 Release 安装 / 保留配置升级")
    result.add_argument("--edition", choices=["clouddrive2", "openlist", "both"], help="both 仅用于两套已有完整实例")
    result.add_argument("--install-dir", help="仅支持单 Edition 的 /opt 下目录；不搬迁现有实例")
    result.add_argument("--check", action="store_true", help="只读实例/版本/HTTPS检查；不收集秘密，不执行安装")
    result.add_argument("--configure-https", action="store_true", help="显式续做/修复 HTTPS；已有域名不切换")
    result.add_argument("--verify", action="store_true", help="另行确认 WebDAV 写入验收；默认不会在升级中运行")
    result.add_argument("--show-credentials", action="store_true", help="仅经确认在私有终端显示已有凭据；不安装、升级或改密")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    log = SafeLog()
    ui = None
    try:
        if args.show_credentials and (args.check or args.configure_https or args.verify):
            raise ValueError("--show-credentials 必须单独使用，不能与检查、HTTPS 修复或验收组合")
        if args.show_credentials and not args.edition:
            raise ValueError("查看凭据请显式指定 --edition，不进入安装/升级选择向导")
        validate_host()
        if args.install_dir and (not args.edition or args.edition == "both"):
            raise ValueError("--install-dir 必须搭配单个 --edition")
        session = LocalSession(log)
        inventory = docker_inventory(session)
        if not args.edition:
            if args.check:
                raise ValueError("只读检查请显式指定 --edition")
            ui = Terminal()
            detected = [product for product in PRODUCTS.values()
                        if {product.bot_container, product.storage_container} & inventory]
            for product in detected:
                log("发现 " + product.display_name + " 容器；选择后仍需核对完整性和实际版本")
            default_choice = "2" if len(detected) == 1 and detected[0].is_openlist else "1"
            chosen = ui.ask("选择 Edition：1 CloudDrive2，2 OpenList，3 升级两套已有实例", default=default_choice)
            args.edition = {"1": "clouddrive2", "2": "openlist", "3": "both"}.get(chosen)
            if not args.edition:
                raise ValueError("Edition 选择无效")
        keys = list(PRODUCTS) if args.edition == "both" else [args.edition]
        instances = [discover(PRODUCTS[key], inventory, session, log, install_dir=args.install_dir) for key in keys]
        if args.edition == "both" and not all(item.present for item in instances):
            raise ValueError("both 只升级两套已有完整实例；首次安装请分别选择 Edition")
        if args.show_credentials:
            if not all(item.present for item in instances):
                raise ValueError("尚无完整实例，不生成凭据、不启动安装")
            if ui is None:
                ui = Terminal()
            for instance in instances:
                show_credentials(ui, instance.product, instance.config, log)
            log("TG2CLOUD_CLI_RESULT=NO_CHANGE；凭据查看结束，未修改实例或配置")
            return 0
        client = ReleaseClient()
        bootstrap = Path(__file__).resolve().parent.parent / ".bootstrap-release.json"
        pinned = json.loads(regular_private(bootstrap)) if bootstrap.exists() else None
        release = client.resolve(pinned["tag"] if pinned else None)
        if pinned and release.commit != pinned["commit"]:
            raise ValueError("引导之后 Release Tag 发生变化；停止，重新下载入口")
        log("正式 Release：" + release.tag + "；固定源码 commit：" + release.commit)
        with tempfile.TemporaryDirectory(prefix="tg2cloud-cli-source-") as private:
            root = Path(private)
            source = client.download(release, root)
            baselines = {release.tag: source}
            plans = []
            for instance in instances:
                old_source = None
                if instance.present:
                    tag = "v" + instance.version
                    if tag not in baselines:
                        old_release = client.resolve(tag)
                        old_dir = root / ("baseline-" + tag)
                        old_dir.mkdir(mode=0o700)
                        baselines[tag] = client.download(old_release, old_dir)
                    old_source = baselines[tag]
                if not args.check and ui is None:
                    ui = Terminal()
                plan = make_plan(instance, release, source, old_source, session, log, ui, configure_https=args.configure_https)
                preview(plan, release, log)
                plans.append((plan, old_source))
            if args.check:
                log("TG2CLOUD_CLI_CHECK=OK；只读预检，不等于部署/WebDAV/公网验收通过")
                return 0
            actionable = [(plan, baseline) for plan, baseline in plans if plan.action in {"install", "upgrade"} or plan.configure_https or args.verify]
            if not actionable:
                log("TG2CLOUD_CLI_RESULT=NO_CHANGE")
                return 0
            log("备份含敏感配置，仅保存在 VPS；失败可能需人工恢复。不要同时运行 EXE 的基础安装/更新。")
            if not ui.confirm("确认执行上述所选计划？所选 Bot 可能短暂中断"):
                log("TG2CLOUD_CLI_RESULT=CANCELLED；未执行安装/升级/HTTPS变更")
                return 0
            for plan, baseline in actionable:
                execute_plan(plan, release, source, baseline, session, log, ui, verify=args.verify)
            log("TG2CLOUD_CLI_RESULT=BASE_HTTPS_OK；WebDAV 是否验收请以独立结果为准")
        return 0
    except KeyboardInterrupt:
        log("TG2CLOUD_CLI_RESULT=INTERRUPTED；请核对安装器回退日志；不假定状态已恢复")
        return 130
    except (ValueError, RuntimeError, TimeoutError, ZoneInfoNotFoundError) as exc:
        log("TG2CLOUD_CLI_RESULT=FAILED：" + str(exc))
        return 1
    except Exception:  # noqa: BLE001 - secret-safe terminal boundary
        # Never dump a traceback containing parsed configuration or raw API data.
        log("TG2CLOUD_CLI_RESULT=FAILED：检查/操作发生异常；未输出配置或凭据，请核对实例状态后重试")
        return 1
    finally:
        if ui is not None:
            ui.close()


def interrupted(_signum: int, _frame) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, interrupted)
    raise SystemExit(main())
