"""底层 SSH/SFTP 封装。paramiko 包装,with 用法。"""

import os
import socket
from pathlib import Path
from typing import Optional

import paramiko


class SSHClient:
    def __init__(
        self,
        host: str,
        port: int = 22,
        user: str = "root",
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        timeout: int = 30,
        banner_timeout: int = 60,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.key_path = key_path
        self.timeout = timeout
        self.banner_timeout = banner_timeout
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None

    def __enter__(self):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname=self.host,
            port=self.port,
            username=self.user,
            timeout=self.timeout,
            banner_timeout=self.banner_timeout,
            auth_timeout=self.timeout,
        )
        if self.key_path:
            kwargs["key_filename"] = self.key_path
        if self.password:
            kwargs["password"] = self.password
            kwargs["allow_agent"] = False
            kwargs["look_for_keys"] = False
        c.connect(**kwargs)
        self._client = c
        self._sftp = c.open_sftp()
        return self

    def __exit__(self, *exc):
        try:
            if self._sftp:
                self._sftp.close()
        finally:
            if self._client:
                self._client.close()

    def exec(self, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
        assert self._client
        _, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    def put_file(self, local: Path, remote: str):
        assert self._sftp
        self._mkdir_p(os.path.dirname(remote))
        self._sftp.put(str(local), remote)

    def put_text(self, content: str, remote: str):
        assert self._sftp
        self._mkdir_p(os.path.dirname(remote))
        with self._sftp.open(remote, "wb") as f:
            f.write(content.encode("utf-8"))

    def get_file(self, remote: str, local: Path) -> bool:
        assert self._sftp
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._sftp.get(remote, str(local))
            return True
        except (FileNotFoundError, IOError):
            return False

    def get_text(self, remote: str) -> Optional[str]:
        assert self._sftp
        try:
            with self._sftp.open(remote, "rb") as f:
                return f.read().decode("utf-8", errors="replace")
        except (FileNotFoundError, IOError):
            return None

    def list_dir(self, remote: str) -> list[str]:
        assert self._sftp
        try:
            return self._sftp.listdir(remote)
        except IOError:
            return []

    def _mkdir_p(self, remote_dir: str):
        assert self._sftp
        if not remote_dir or remote_dir == "/":
            return
        parts = remote_dir.strip("/").split("/")
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}"
            try:
                self._sftp.mkdir(cur)
            except IOError:
                pass


def low_level_banner_test(host: str, port: int = 22, timeout: int = 30) -> tuple[bool, str]:
    """绕过 paramiko,直接 socket 读 SSH banner。
    用来分清:网络层不通 vs paramiko 协议层失败。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        banner = s.recv(255).decode("utf-8", errors="replace").strip()
        return True, banner if banner else "(空 banner — 服务器没说话)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass


def test_connection(
    host: str,
    port: int = 22,
    user: str = "root",
    password: Optional[str] = None,
    key_path: Optional[str] = None,
) -> tuple[bool, str]:
    """测试 SSH 是否能连。失败时附带底层 socket 的 banner 探测,方便定位。"""
    try:
        with SSHClient(host, port, user, password, key_path) as ssh:
            rc, out, err = ssh.exec("echo OK && uname -a", timeout=10)
            if rc == 0:
                return True, out.strip()
            return False, f"rc={rc}, err={err}"
    except Exception as e:
        low_ok, low_msg = low_level_banner_test(host, port)
        if low_ok:
            return False, (
                f"paramiko 失败:{type(e).__name__}: {e}\n\n"
                f"底层 TCP+banner 读取 OK:服务器 banner = {low_msg!r}\n"
                f"→ 网络通,SSH 服务也活着。问题在 paramiko 握手层(通常是密钥协商算法或者认证)。"
            )
        else:
            return False, (
                f"paramiko 失败:{type(e).__name__}: {e}\n\n"
                f"底层 socket 测试也失败:{low_msg}\n"
                f"→ 网络层 / 防火墙问题。先确认 VPS 22 端口对外开放且 sshd 在跑。"
            )


def verify_v31_env(
    host: str,
    port: int = 22,
    user: str = "root",
    password: Optional[str] = None,
    key_path: Optional[str] = None,
) -> tuple[bool, list[dict]]:
    """通过 SSH 逐一检查 Worker 的 V3.1 环境组件。

    返回 (all_ok, list_of_checks)。每个 check 是 dict(name, ok, msg)。
    all_ok=True 表示 SSH 连通且全部 V3.1 组件已装好。
    """
    checks: list[dict] = []
    all_ok = True
    try:
        with SSHClient(host, port, user, password, key_path) as ssh:
            # 1. ffmpeg / ffprobe
            for name, cmd in (
                ("ffmpeg", "ffmpeg -version | head -1"),
                ("ffprobe", "ffprobe -version | head -1"),
            ):
                try:
                    rc, out, _ = ssh.exec(cmd, timeout=10)
                    if rc == 0 and out.strip():
                        checks.append({"name": name, "ok": True, "msg": out.strip().splitlines()[0]})
                    else:
                        checks.append({"name": name, "ok": False, "msg": "未安装"})
                        all_ok = False
                except Exception as e:
                    checks.append({"name": name, "ok": False, "msg": str(e)[:200]})
                    all_ok = False

            # 2. gost
            try:
                rc, out, _ = ssh.exec("/usr/local/bin/gost -V 2>&1", timeout=10)
                if rc == 0 and out.strip():
                    checks.append({"name": "gost 中继", "ok": True, "msg": out.strip()})
                else:
                    checks.append({"name": "gost 中继", "ok": False, "msg": "未安装 /usr/local/bin/gost"})
                    all_ok = False
            except Exception as e:
                checks.append({"name": "gost 中继", "ok": False, "msg": str(e)[:200]})
                all_ok = False

            # 3. venv + V3.1 Python 库(一次导入,分列结果)
            venv_python = "/opt/twitter-worker/venv/bin/python"
            import_script = (
                f"{venv_python} -c \"\"\""
                "import sys\n"
                "mods=['playwright','playwright_stealth','yt_dlp','curl_cffi','pyotp']\n"
                "for m in mods:\n"
                "    try:\n"
                "        __import__(m)\n"
                "        print(m+':OK')\n"
                "    except Exception as e:\n"
                "        print(m+':FAIL:'+str(e))\n"
                "\"\"\""
            )
            try:
                rc, out, _ = ssh.exec(import_script, timeout=30)
                if rc == 0:
                    for line in out.strip().splitlines():
                        if ":" not in line:
                            continue
                        mod, status = line.split(":", 1)
                        if status.startswith("OK"):
                            checks.append({"name": mod, "ok": True, "msg": "导入 OK"})
                        elif status.startswith("FAIL:"):
                            checks.append({"name": mod, "ok": False, "msg": status[5:]})
                            all_ok = False
                        else:
                            checks.append({"name": mod, "ok": True, "msg": status})
                else:
                    checks.append({
                        "name": "venv Python", "ok": False,
                        "msg": f"venv 不存在或 Python 失败: {out[:200]}"
                    })
                    all_ok = False
            except Exception as e:
                checks.append({"name": "venv Python", "ok": False, "msg": str(e)[:200]})
                all_ok = False

            # 4. chromium 浏览器缓存
            try:
                rc, out, _ = ssh.exec(
                    "ls /root/.cache/ms-playwright/chromium-*/chrome-linux/chrome 2>/dev/null | head -1",
                    timeout=10,
                )
                if rc == 0 and out.strip():
                    checks.append({"name": "Chromium 浏览器", "ok": True, "msg": "已缓存"})
                else:
                    checks.append({
                        "name": "Chromium 浏览器", "ok": False,
                        "msg": "未安装,请运行: /opt/twitter-worker/venv/bin/playwright install chromium",
                    })
                    all_ok = False
            except Exception as e:
                checks.append({"name": "Chromium 浏览器", "ok": False, "msg": str(e)[:200]})
                all_ok = False

    except Exception as e:
        all_ok = False
        checks.append({"name": "SSH 连接", "ok": False, "msg": str(e)[:200]})
    return all_ok, checks
