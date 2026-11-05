"""旧的独立用户建表脚本，已废弃。请使用项目根目录的 create_all_tables.py 统一建表。"""
import os
os.system(f"python {os.path.dirname(__file__)}/../create_all_tables.py")
