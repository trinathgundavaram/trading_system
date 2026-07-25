"""Version snapshot stored on every signal/trade_snapshot so you can always
answer "which exact rules/weights/prompt produced this decision?" - essential
once Bayesian updates start changing weights over time."""
import hashlib
import os
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass
class ModelVersionSnapshot:
    rule_engine_version: str
    weight_version: str
    regime_version: str
    prompt_version: str
    threshold_version: str
    pattern_db_version: str


def _hash_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except FileNotFoundError:
        return "unversioned"


def get_current_versions(cfg: dict, prompt_template_path: str = None) -> ModelVersionSnapshot:
    """Called at signal generation time. Reads static versions from config.yaml
    (bumped manually when you change rule weights/regime logic/thresholds) and
    computes dynamic ones (prompt hash, date-based pattern DB snapshot ID)."""
    system_cfg = cfg.get("system", {})
    prompt_template_path = prompt_template_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "trade_prompt.md"
    )
    return ModelVersionSnapshot(
        rule_engine_version=system_cfg.get("rule_engine_version", "unversioned"),
        weight_version=f"weights_{datetime.utcnow().strftime('%Y_%m_%d_%H%M')}",
        regime_version=system_cfg.get("regime_algorithm_version", "unversioned"),
        prompt_version=_hash_file(prompt_template_path),
        threshold_version=system_cfg.get("threshold_version", "unversioned"),
        pattern_db_version=f"pdb_{datetime.utcnow().strftime('%Y_%m_%d')}",
    )


def versions_as_dict(cfg: dict, prompt_template_path: str = None) -> dict:
    return asdict(get_current_versions(cfg, prompt_template_path))
