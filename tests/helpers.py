"""Shared fixtures: a config, synthetic request records, and a tiny fake JPetStore (Spring MVC) source tree."""
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config  # noqa: E402

CFG = load_config()

CONTROLLER = '''package x;
@Controller
@RequestMapping("/catalog")
public class CatalogController {
  @GetMapping("/viewCategory")
  public String viewCategory() {
    return null;
  }
  @GetMapping("/viewProduct")
  public String viewProduct() {
    products = catalogService.getItemListByProduct(id);
    return null;
  }
}
'''


def make_records(n_seconds=60, rps=20, spike=None, fail_endpoint=None, seed=7):
    """Steady ~60 ms traffic across 3 endpoints; optional latency spike (start, end, ms); optional failing endpoint."""
    rnd = random.Random(seed)
    names = ["GET /catalog/viewCategory", "GET /catalog/viewProduct", "GET /cart"]
    t0, recs = 1_800_000_000.0, []
    for s in range(n_seconds):
        for k in range(rps):
            ts = t0 + s + k / rps
            name = names[k % 3]
            rt = 40 + rnd.random() * 40
            if spike and spike[0] <= s < spike[1] and name == names[1]:
                rt += spike[2]
            ok = not (fail_endpoint == name and s > 30 and k % 5 == 0)
            recs.append({"ts": ts, "name": name, "method": "GET", "rt": rt, "ok": ok, "status": 200 if ok else 500,
                         "err": "" if ok else "HTTPError('500 Server Error')", "bytes": 1000})
    return recs, t0


def make_metrics(t0, n_seconds=60, cpu=30.0):
    return [{"ts": t0 + i, "cpu_pct": cpu, "mem_pct": 50.0, "net_sent_kbs": 100.0, "net_recv_kbs": 200.0, "java_cpu_pct": cpu / 2,
             "java_rss_mb": 400.0 + i, "java_threads": 40, "locust_cpu_pct": 5.0} for i in range(n_seconds)]


def make_repo():
    """A minimal fake Spring MVC project so find_handler/grep/read_file can be tested."""
    root = Path(tempfile.mkdtemp())
    d = root / "src/main/java/org/mybatis/jpetstore/web/controllers"
    d.mkdir(parents=True)
    (d / "CatalogController.java").write_text(CONTROLLER, encoding="utf-8")
    return root
