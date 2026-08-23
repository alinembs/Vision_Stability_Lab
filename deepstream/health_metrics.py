import time
import psutil
import logging

logger = logging.getLogger("HealthMetrics")

class HealthMetricsCollector:
    def __init__(self):
        self.process = psutil.Process()
        self.warmup_rss = self.process.memory_info().rss / (1024 * 1024)
        logger.info(f"Baseline RSS após warm-up: {self.warmup_rss:.2f} MB")

    def collect(self):
        try:
            mem_info = self.process.memory_info()
            rss_mb = mem_info.rss / (1024 * 1024)
            vms_mb = mem_info.vms / (1024 * 1024)
            threads = self.process.num_threads()
            open_fds = self.process.num_fds() if hasattr(self.process, 'num_fds') else 0

            system_mem = psutil.virtual_memory()

            metrics = {
                "timestamp": time.time(),
                "process": {
                    "rss_mb": round(rss_mb, 2),
                    "vms_mb": round(vms_mb, 2),
                    "memory_growth_mb": round(rss_mb - self.warmup_rss, 2),
                    "threads": threads,
                    "open_fds": open_fds
                },
                "system": {
                    "ram_used_mb": round(system_mem.used / (1024 * 1024), 2),
                    "ram_available_mb": round(system_mem.available / (1024 * 1024), 2),
                    "cpu_percent": psutil.cpu_percent(interval=None)
                }
            }
            return metrics
        except Exception as e:
            logger.error(f"Erro ao coletar métricas de saúde: {e}")
            return None
