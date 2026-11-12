import json

from src.event_intelligence import reconcile_qwen_corroboration


if __name__ == "__main__":
    print(json.dumps(reconcile_qwen_corroboration(min_domains=2), ensure_ascii=False, indent=2))
