"""ID / 码生成工具。"""
import random

# 去掉易混字符（0/O、1/I/L）
INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def gen_invite_code(length: int = 6) -> str:
    return "".join(random.choice(INVITE_ALPHABET) for _ in range(length))
