"""Small Windows helpers shared by setup and doctor: run a PowerShell
script (optionally elevated, via one UAC prompt) and read back its output,
and inspect the capture Scheduled Task."""

import json
import subprocess
import tempfile
from pathlib import Path

from vrmon import config


def powershell(script: str, timeout: float = 120) -> tuple[int, str]:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
         "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=config.NO_WINDOW,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def powershell_elevated(script: str, timeout: float = 600) -> tuple[bool, str]:
    """Runs a script as administrator - Windows shows one UAC prompt. Returns
    (ran, output); ran is False if the prompt was declined."""
    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "elevated.ps1"
        out_path = Path(tmp) / "elevated.log"
        script_path.write_text(
            f"try {{\n{script}\n}} catch {{ \"ERROR: $_\" }}\n",
            encoding="utf-8-sig",
        )
        # Created here, unelevated, so it stays readable by us: a file the
        # elevated process creates itself is owned by Administrators.
        # Overwriting an existing file keeps its permissions.
        out_path.write_text("", encoding="utf-8")
        launcher = (
            f"$p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
            f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',"
            f"'& \"{script_path}\" *> \"{out_path}\"'; exit $p.ExitCode"
        )
        code, out = powershell(launcher, timeout=timeout)
        if "canceled by the user" in out or "operation was canceled" in out.lower():
            return False, out  # UAC prompt declined
        return True, _decode_redirected(out_path.read_bytes()).strip() or out


def _decode_redirected(data: bytes) -> str:
    """Windows PowerShell 5.1's `*>` redirection writes UTF-16 with a BOM."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def capture_task() -> dict | None:
    """The capture Scheduled Task's action and settings, or None if it
    doesn't exist."""
    code, out = powershell(
        f"$t = Get-ScheduledTask -TaskName '{config.PRESENTMON_TASK_NAME}' -ErrorAction SilentlyContinue\n"
        "if ($t) { [pscustomobject]@{ Execute = $t.Actions[0].Execute; Arguments = $t.Actions[0].Arguments;"
        " RunLevel = [string]$t.Principal.RunLevel; MultipleInstances = [string]$t.Settings.MultipleInstances }"
        " | ConvertTo-Json -Compress }"
    )
    if code != 0 or not out.startswith("{"):
        return None
    return json.loads(out)


def authenticode_signer(path: Path) -> tuple[str, str]:
    """(status, signer subject) of a file's digital signature, e.g.
    ("Valid", "CN=Intel Corporation, ...")."""
    code, out = powershell(
        f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
        "\"$($s.Status)|$($s.SignerCertificate.Subject)\""
    )
    status, _, signer = out.partition("|")
    return status.strip(), signer.strip()
