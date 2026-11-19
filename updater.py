import os
import sys
import re
import shutil
import tempfile
import hashlib
import subprocess
from urllib.parse import urlparse

from theme import logger

CURRENT_VERSION = "1.0.0"
UPDATE_CHECK_URL = "https://api.github.com/repos/KyleKo-F/Ks-Trade-Journal/releases/latest"
ALLOWED_UPDATE_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")


def is_trusted_update_url(url):
    if not url:
        return False
    try:
        parsed = urlparse(str(url).strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(
            host == allowed or host.endswith("." + allowed)
            for allowed in ALLOWED_UPDATE_HOSTS
        )
    except Exception:
        return False


def _parse_semver(ver_str):
    cleaned = re.sub(r"^[vV]+", "", str(ver_str or "").strip())
    if not cleaned:
        return None
    core_str, _, pre_str = cleaned.partition("-")
    core_parts = [int(p) for p in re.findall(r"\d+", core_str)]
    if not core_parts:
        return None
    while len(core_parts) < 3:
        core_parts.append(0)
    if not pre_str:
        return tuple(core_parts), 1, ()
    pre_parts = []
    for seg in re.split(r"[.\-_]+", pre_str.lower()):
        if not seg:
            continue
        if seg.isdigit():
            pre_parts.append((0, int(seg), ""))
        else:
            pre_parts.append((1, 0, seg))
    return tuple(core_parts), 0, tuple(pre_parts)


def is_newer_version(latest, current=CURRENT_VERSION):
    try:
        l_parsed = _parse_semver(latest)
        c_parsed = _parse_semver(current)
        if l_parsed is not None and c_parsed is not None:
            l_core, l_stable, l_pre = l_parsed
            c_core, c_stable, c_pre = c_parsed
            max_len = max(len(l_core), len(c_core))
            l_core = l_core + (0,) * (max_len - len(l_core))
            c_core = c_core + (0,) * (max_len - len(c_core))
            if l_core != c_core:
                return l_core > c_core
            if l_stable != c_stable:
                return l_stable > c_stable
            return l_pre > c_pre
    except Exception:
        pass
    return str(latest) > str(current)


def check_latest_release(http_session, current_version=CURRENT_VERSION):
    """Queries GitHub latest release API and returns structured status dict."""
    res = http_session.get(
        UPDATE_CHECK_URL,
        headers={"Accept": "application/vnd.github+json"},
        timeout=6,
    )
    if res.status_code != 200:
        return {"status": "http_error", "status_code": res.status_code}

    data = res.json()
    raw_tag = str(data.get("tag_name") or data.get("name") or "").strip()
    latest_tag = re.sub(r'^[vV]+', '', raw_tag)
    release_notes = str(data.get("body", "") or "").strip()
    assets = data.get("assets", [])
    download_url = None
    checksum_url = None
    asset_digest = ""

    for asset in assets:
        name_lower = str(asset.get("name", "")).lower()
        dl = asset.get("browser_download_url")
        if name_lower.endswith(".exe") and (download_url is None or "journal" in name_lower):
            download_url = dl
            raw_digest = str(asset.get("digest") or "").strip()
            if raw_digest.lower().startswith("sha256:"):
                cand = raw_digest.split(":", 1)[1].strip().lower()
                if re.fullmatch(r"[a-f0-9]{64}", cand):
                    asset_digest = cand
        elif (name_lower.endswith(".sha256") or "sha256" in name_lower or "checksum" in name_lower) and checksum_url is None:
            checksum_url = dl

    if (
        latest_tag
        and download_url
        and is_trusted_update_url(download_url)
        and (not checksum_url or is_trusted_update_url(checksum_url))
        and is_newer_version(latest_tag, current_version)
    ):
        return {
            "status": "update_available",
            "latest_tag": latest_tag,
            "download_url": download_url,
            "checksum_url": checksum_url or "",
            "expected_sha256": asset_digest,
            "release_notes": release_notes,
        }

    return {"status": "up_to_date", "latest_tag": latest_tag, "release_notes": release_notes}


def download_and_install_update(http_session, download_url, checksum_url=None, expected_sha256="", progress_cb=None):
    """Downloads, optionally SHA-256 verifies, and launches the replacement installer batch script."""
    if not is_trusted_update_url(download_url) or (checksum_url and not is_trusted_update_url(checksum_url)):
        raise ValueError("Untrusted or missing update/checksum URL.")

    temp_dir = None
    try:
        verified_sha256 = ""
        if checksum_url:
            cs_res = http_session.get(checksum_url, timeout=10)
            cs_res.raise_for_status()
            match = re.search(r"\b([a-fA-F0-9]{64})\b", cs_res.text)
            if not match:
                raise ValueError("Release checksum file did not contain a valid SHA-256 digest.")
            verified_sha256 = match.group(1).lower()
        elif expected_sha256 and re.fullmatch(r"[a-fA-F0-9]{64}", str(expected_sha256).strip()):
            verified_sha256 = str(expected_sha256).strip().lower()
        if not verified_sha256:
            raise ValueError("Release did not provide a valid SHA-256 checksum for integrity verification.")

        res = http_session.get(download_url, stream=True, timeout=30)
        res.raise_for_status()
        if not is_trusted_update_url(res.url):
            raise ValueError("Update redirected to an untrusted host.")

        try:
            total_len = int(res.headers.get("content-length") or 0)
        except (ValueError, TypeError):
            total_len = 0

        if progress_cb:
            progress_cb(0.0 if total_len > 0 else None)

        temp_dir = tempfile.mkdtemp(prefix="ks_upd_")
        temp_exe = os.path.join(temp_dir, "ks_trade_journal_update.exe")

        hasher = hashlib.sha256()
        downloaded = 0
        with open(temp_exe, "wb") as f:
            for chunk in res.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total_len > 0:
                        progress_cb(min(1.0, downloaded / total_len))

        with open(temp_exe, "rb") as chk_f:
            if chk_f.read(2) != b"MZ":
                raise ValueError("Downloaded update is not a valid Windows executable.")

        if verified_sha256 and hasher.hexdigest().lower() != verified_sha256:
            raise ValueError("Downloaded update failed SHA-256 integrity verification.")

        if progress_cb:
            progress_cb(1.0)

        current_exe = sys.executable
        if (
            any(ch in current_exe for ch in ("\r", "\n", '"'))
            or any(ch in temp_exe for ch in ("\r", "\n", '"'))
            or any(ch in temp_dir for ch in ("\r", "\n", '"'))
        ):
            raise ValueError("Unsafe executable path characters detected.")

        bat_script = os.path.join(temp_dir, "update_installer.bat")
        with open(bat_script, "w", encoding="utf-8") as f:
            f.write(
                "@echo off\n"
                'cd /d "%TEMP%"\n'
                "set RETRY=0\n"
                ":retry_copy\n"
                "timeout /t 1 /nobreak > nul\n"
                'copy /y "%KS_UPD_SRC%" "%KS_UPD_DST%" > nul 2>&1\n'
                "if errorlevel 1 (\n"
                "  set /a RETRY+=1\n"
                "  if %RETRY% lss 15 goto retry_copy\n"
                "  goto cleanup\n"
                ")\n"
                'start "" "%KS_UPD_DST%"\n'
                ":cleanup\n"
                'del /f /q "%KS_UPD_SRC%" > nul 2>&1\n'
                'start "" /b cmd /d /c "timeout /t 3 /nobreak > nul & rmdir /s /q "%KS_UPD_DIR%""\n'
                'del /f /q "%~f0"\n'
            )

        proc_env = os.environ.copy()
        proc_env["KS_UPD_SRC"] = temp_exe
        proc_env["KS_UPD_DST"] = current_exe
        proc_env["KS_UPD_DIR"] = temp_dir

        subprocess.Popen(
            ["cmd.exe", "/c", bat_script],
            env=proc_env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
    except Exception:
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
