from tests.tasks.smoke import run_smoke


def test_real_http_process_start_restart_and_offline_download():
    assert run_smoke()["status"] == "PASS"
