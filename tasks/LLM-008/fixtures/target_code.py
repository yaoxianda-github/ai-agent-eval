def validate_email(email: str) -> bool:
    """
    验证邮箱地址格式是否合法
    """
    if not email or not isinstance(email, str):
        return False

    email = email.strip()

    if len(email) > 254:
        return False

    if '@' not in email:
        return False

    local_part, domain = email.rsplit('@', 1)

    if not local_part or len(local_part) > 64:
        return False

    if not domain or '.' not in domain:
        return False

    if local_part.startswith('.') or local_part.endswith('.'):
        return False

    if '..' in local_part:
        return False

    domain_parts = domain.split('.')
    for part in domain_parts:
        if not part or part.startswith('-') or part.endswith('-'):
            return False
        if not part.replace('-', '').isalnum():
            return False

    allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'*+-/=?^_`{|}~.")
    if not set(local_part).issubset(allowed_chars):
        return False

    return True


def parse_date(date_str: str, fmt: str = "%Y-%m-%d"):
    """
    解析日期字符串，返回 datetime 对象，解析失败返回 None
    """
    from datetime import datetime

    if not date_str or not isinstance(date_str, str):
        return None

    date_str = date_str.strip()

    try:
        return datetime.strptime(date_str, fmt)
    except (ValueError, TypeError):
        return None


def paginate(items: list, page: int = 1, page_size: int = 10) -> dict:
    """
    对列表进行分页，返回分页结果
    """
    if not items:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "total_pages": 0
        }

    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 10
    if page_size > 100:
        page_size = 100

    total = len(items)
    total_pages = (total + page_size - 1) // page_size

    if page > total_pages:
        page = total_pages

    start = (page - 1) * page_size
    end = start + page_size

    return {
        "items": items[start:end],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }
