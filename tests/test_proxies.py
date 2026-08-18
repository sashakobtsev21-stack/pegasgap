"""Тесты пула прокси."""

from datetime import datetime, timedelta

from pegasgap.proxies import Proxy, ProxyPool, is_blocked, load_pool, parse_line

NOW = datetime(2026, 8, 18, 12, 0, 0)


def test_line_in_the_format_providers_actually_hand_out():
    p = parse_line("45.133.223.106:8000:user:secret")
    assert (p.host, p.port, p.user, p.password) == ("45.133.223.106", 8000, "user", "secret")


def test_line_without_auth():
    p = parse_line("203.0.113.10:3128")
    assert (p.host, p.port) == ("203.0.113.10", 3128)
    assert p.url == "http://203.0.113.10:3128"


def test_garbage_and_comments_are_skipped():
    assert parse_line("") is None
    assert parse_line("# комментарий") is None
    assert parse_line("не-прокси") is None
    assert parse_line("host:порт:u:p") is None


def test_credentials_never_reach_the_logs():
    """Единственная форма прокси для логов — host:port. Пароль в сообщении об ошибке
    осел бы в файле лога навсегда, а лог читают и пересылают."""
    p = Proxy("203.0.113.10", 8000, "user", "secret")
    assert str(p) == "203.0.113.10:8000"
    assert "secret" not in str(p)
    assert "secret" in p.url          # но в сам httpx уходит полный адрес


def test_pool_hands_out_in_a_circle():
    a, b = Proxy("a", 1), Proxy("b", 2)
    pool = ProxyPool([a, b])
    assert [pool.acquire(NOW) for _ in range(4)] == [a, b, a, b]


def test_empty_pool_means_work_directly():
    """Нет прокси — не ошибка: инструмент работает напрямую, просто упирается раньше."""
    assert ProxyPool([]).acquire() is None
    assert not ProxyPool([])


def test_burned_proxy_cools_down_instead_of_being_dropped():
    """Засвеченный прокси не сломан — через несколько минут он снова годен. Выбрасывать
    насовсем значило бы за один обход сточить весь список."""
    a, b = Proxy("a", 1), Proxy("b", 2)
    pool = ProxyPool([a, b])
    pool.penalise(a, NOW)
    assert [pool.acquire(NOW) for _ in range(3)] == [b, b, b]
    later = NOW + timedelta(hours=1)
    assert a in {pool.acquire(later) for _ in range(4)}


def test_when_everything_cools_we_retry_rather_than_stall():
    """Пауза всего обхода хуже, чем повторная попытка через подостывший адрес."""
    a = Proxy("a", 1)
    pool = ProxyPool([a])
    pool.penalise(a, NOW)
    assert pool.acquire(NOW) is a


def test_stats_report_availability():
    a, b = Proxy("a", 1), Proxy("b", 2)
    pool = ProxyPool([a, b])
    pool.penalise(a, NOW)
    assert pool.stats(NOW) == {"total": 2, "cooling": 1, "available": 1}


def test_only_address_level_refusals_burn_a_proxy():
    """Кривые параметры и таймаут поиска к адресу отношения не имеют — наказывать за них
    значит сточить пул на ровном месте."""
    assert is_blocked("HTTP 401")
    assert is_blocked("modsearch.php: HTTP 429")
    assert is_blocked("превышен лимит кол-ва поисковых запросов")
    assert not is_blocked("Поиск не завершился за отведённое время.")
    assert not is_blocked("страна не найдена")


def test_missing_file_is_not_an_error(tmp_path):
    assert len(load_pool(tmp_path / "нет.txt")) == 0


def test_file_is_read_in_the_pasted_format(tmp_path):
    f = tmp_path / "proxies.txt"
    f.write_text("# список\n5.8.14.90:9265:u:p\n\n45.133.223.106:8000:u2:p2\nмусор\n",
                 encoding="utf-8")
    pool = load_pool(f)
    assert len(pool) == 2
    assert str(pool.acquire(NOW)) == "5.8.14.90:9265"
