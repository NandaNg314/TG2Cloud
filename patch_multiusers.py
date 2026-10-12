from pathlib import Path
import os

# Patch config.py
cfg_path = Path("/opt/tg2cloud-openlist/app/config.py")
cfg = cfg_path.read_text(encoding="utf-8")

if "allowed_user_ids: tuple[int, ...]" not in cfg:
    cfg = cfg.replace(
        'rclone_remote_name: str = "cd2"',
        'rclone_remote_name: str = "cd2"\n    allowed_user_ids: tuple[int, ...] = ()'
    )

    old_check = """        if allowed_user_id <= 0:
            raise RuntimeError("ALLOWED_USER_ID 必须大于 0")"""
    new_check = """        if allowed_user_id <= 0:
            raise RuntimeError("ALLOWED_USER_ID 必须大于 0")
        allowed_user_ids_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
        if allowed_user_ids_raw:
            try:
                allowed_user_ids = tuple(int(x.strip()) for x in allowed_user_ids_raw.split(",") if x.strip())
            except ValueError:
                allowed_user_ids = (allowed_user_id,)
        else:
            allowed_user_ids = (allowed_user_id,)"""
    cfg = cfg.replace(old_check, new_check)

    cfg = cfg.replace(
        "destination_label=destination_label,\n            rclone_remote_name=rclone_remote_name,",
        "destination_label=destination_label,\n            rclone_remote_name=rclone_remote_name,\n            allowed_user_ids=allowed_user_ids,"
    )
    cfg_path.write_text(cfg, encoding="utf-8")
    print("config.py patched successfully")
else:
    print("config.py already patched")

# Patch main.py
main_path = Path("/opt/tg2cloud-openlist/app/main.py")
main = main_path.read_text(encoding="utf-8")

if "self.settings.allowed_user_ids" not in main:
    old_auth = """    def _authorized(self, sender_id: int | None) -> bool:
        return sender_id == self.settings.allowed_user_id"""
    new_auth = """    def _authorized(self, sender_id: int | None) -> bool:
        if sender_id is None:
            return False
        return sender_id in self.settings.allowed_user_ids"""
    main = main.replace(old_auth, new_auth)

    old_cb = """    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        if (
            not self._authorized(event.sender_id)
            or getattr(event, "chat_id", None) != self.settings.allowed_user_id
        ):"""
    new_cb = """    async def _on_callback(self, event: events.CallbackQuery.Event) -> None:
        if (
            not self._authorized(event.sender_id)
            or getattr(event, "chat_id", None) not in self.settings.allowed_user_ids
        ):"""
    main = main.replace(old_cb, new_cb)
    main_path.write_text(main, encoding="utf-8")
    print("main.py patched successfully")
else:
    print("main.py already patched")
