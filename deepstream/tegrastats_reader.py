import subprocess
import logging

logger = logging.getLogger("TegrastatsReader")

class TegrastatsReader:
    def __init__(self):
        self.available = False
        try:
            res = subprocess.run(["which", "tegrastats"], capture_output=True, text=True)
            if res.returncode == 0:
                self.available = True
        except Exception:
            pass

    def read_latest(self):
        if not self.available:
            return {"status": "unavailable"}

        try:
            res = subprocess.run(["tegrastats", "--interval", "1000"], capture_output=True, text=True, timeout=1)
            if res.returncode == 0 and res.stdout:
                return {"raw": res.stdout.strip(), "status": "ok"}
        except Exception as e:
            logger.debug(f"Erro ao ler tegrastats: {e}")

        return {"status": "error"}
