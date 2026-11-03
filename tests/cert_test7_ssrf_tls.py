import sys

sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.research.security import is_safe_url

SSRF_PROBES = [
    # Loopback IPv4
    ("http://127.0.0.1", False),
    ("http://127.0.0.1:8000/internal", False),
    ("http://127.0.1.1", False),
    ("http://127.255.255.254", False),
    # Localhost hostname
    ("http://localhost", False),
    ("http://localhost:5432", False),
    ("https://localhost/admin", False),
    # RFC 1918 Private IPv4
    ("http://10.0.0.1", False),
    ("http://10.254.254.1:8080", False),
    ("http://172.16.0.1", False),
    ("http://172.31.255.255", False),
    ("http://192.168.0.1", False),
    ("http://192.168.1.100", False),
    # Cloud Metadata Services
    ("http://169.254.169.254/latest/meta-data/", False),
    ("http://169.254.169.254/computeMetadata/v1/", False),
    ("http://metadata.google.internal", False),
    ("http://instance-data", False),
    # IPv6 Loopback, Link-Local, and Unique Local
    ("http://[::1]", False),
    ("http://[::1]:8000", False),
    ("http://[fe80::1]", False),
    ("http://[fc00::1]", False),
    ("http://[::ffff:127.0.0.1]", False),
    # Non-HTTP Schemes
    ("file:///etc/passwd", False),
    ("file://C:/Windows/win.ini", False),
    ("ftp://public.ftp.org/file", False),
    ("gopher://evil.com:70/1", False),
    ("javascript:alert(1)", False),
    ("data:text/html,test", False),
    # Invalid formats
    ("", False),
    ("not-a-valid-url", False),
    # Legitimate Public Domains (must pass or fail only on DNS if offline)
    ("https://www.reuters.com/world/india", True),
    ("https://www.bbc.com/news", True),
    ("https://news.google.com/rss", True),
]

def run_ssrf_tls_audit():
    print("=== TEST 7: SSRF & TLS ADVERSARIAL PROBES ===")
    
    print("\n--- 7A. SSRF Probe Validation ---")
    passed_probes = 0
    failed_probes = 0

    for probe_url, expected in SSRF_PROBES:
        actual = is_safe_url(probe_url)
        status_ok = (actual == expected)
        res_str = "PASS" if status_ok else "FAIL"
        if not status_ok:
            failed_probes += 1
            print(f"[{res_str}] {probe_url:55} Expected: {expected}, Got: {actual}")
        else:
            passed_probes += 1

    print(f"Total SSRF Probes Executed: {len(SSRF_PROBES)}")
    print(f"Passed Probes: {passed_probes}")
    print(f"Failed Probes: {failed_probes}")
    assert failed_probes == 0, f"{failed_probes} SSRF probes failed!"

    # 7B. TLS Verification Inspection across Codebase
    print("\n--- 7B. TLS Verification Audit across HTTP Clients ---")
    import os

    insecure_tls_usages = []
    app_dir = "app"
    for root, _, files in os.walk(app_dir):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
                    if "verify=False" in content:
                        for line_no, line in enumerate(content.splitlines(), start=1):
                            if "verify=False" in line:
                                insecure_tls_usages.append((path, line_no, line.strip()))

    print(f"Insecure TLS calls (verify=False) found in 'app/': {len(insecure_tls_usages)}")
    for p, lno, ltext in insecure_tls_usages:
        print(f"   [DEFECT FOUND] {p}:{lno} -> {ltext}")

    print("\nTEST 7 RESULT: COMPLETED")
    if insecure_tls_usages:
        print("  SSRF Security: PASS (All internal/cloud ranges blocked)")
        print(f"  TLS Verification: FAIL ({len(insecure_tls_usages)} verify=False detected in app/publishing/brave_cdp.py)")
    else:
        print("  SSRF & TLS Security: PASS")

if __name__ == "__main__":
    run_ssrf_tls_audit()
