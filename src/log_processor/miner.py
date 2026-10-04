"""
Drain3 Log Template Mining and Baseline Diffing Engine.
Academic fidelity to LogSage (arXiv:2506.03691):
- Uses Drain3 with custom masking rules from config/drain3.ini
- Trains baseline templates from x=3 success runs
- Performs template diffing to isolate novel templates in failed runs
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig


DEFAULT_CONFIG_PATH = "config/drain3.ini"
DEFAULT_BASELINES_DIR = "data/baselines"


class DrainBaselineMiner:
    """
    Manages Drain3 template mining and diffing against baseline templates.
    Can be run globally or segmented per-service.
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self.miner_config = TemplateMinerConfig()
        if os.path.exists(config_path):
            self.miner_config.load(config_path)

        self.miner = TemplateMiner(config=self.miner_config)
        # Set of baseline template strings mined from x=3 successful runs
        self.baseline_templates: Set[str] = set()

    def train_on_lines(self, lines: List[str]) -> Set[str]:
        """Ingests log lines and registers their template clusters."""
        new_templates = set()
        for line in lines:
            line_clean = line.strip()
            if not line_clean:
                continue
            res = self.miner.add_log_message(line_clean)
            template = res.get("template_mined")
            if template:
                new_templates.add(template)
        return new_templates

    def train_baseline_from_runs(self, run_telemetries: List[Dict[str, List[str]]]) -> Set[str]:
        """
        Trains baseline template clusters on x=3 success runs.
        run_telemetries is a list of {service_name: [log_lines]} dictionaries.
        """
        for run_logs in run_telemetries:
            for service, lines in run_logs.items():
                self.train_on_lines(lines)

        # Record all cluster templates currently in the miner as baseline templates
        self.baseline_templates = {
            cluster.get_template() for cluster in self.miner.drain.id_to_cluster.values()
        }
        return self.baseline_templates

    def save_baselines(self, output_path: str = f"{DEFAULT_BASELINES_DIR}/drain3_baseline_templates.json") -> None:
        """Saves mined baseline templates to JSON."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        data = {
            "total_templates": len(self.baseline_templates),
            "templates": sorted(list(self.baseline_templates)),
        }
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2)

    def load_baselines(self, input_path: str = f"{DEFAULT_BASELINES_DIR}/drain3_baseline_templates.json") -> bool:
        """Loads baseline templates from disk if available."""
        if not os.path.exists(input_path):
            return False
        with open(input_path, "r") as f:
            data = json.load(f)
            self.baseline_templates = set(data.get("templates", []))
            # Also register into miner
            for template in self.baseline_templates:
                self.miner.add_log_message(template)
        return True

    def classify_line(self, line: str) -> Tuple[str, bool]:
        """
        Classifies a single log line using Drain3.
        Returns:
            (template_string, is_novel)
            is_novel is True if this template was never seen in the baseline.
        """
        line_clean = line.strip()
        if not line_clean:
            return "", False

        res = self.miner.add_log_message(line_clean)
        template = res.get("template_mined", "")
        # A template is novel if it is NOT in the baseline template set
        is_novel = template not in self.baseline_templates
        return template, is_novel
