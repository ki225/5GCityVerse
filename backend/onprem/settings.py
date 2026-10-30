"""Process configuration; no clients or stores are created at import time."""
from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

@dataclass(frozen=True)
class ApiSettings:
    state_file: Path
    ue_state_file: Path
    round_state_file: Path
    cors_origins: tuple[str, ...]
    canonical_reports: bool = False

    @classmethod
    def from_environment(cls):
        load_dotenv(PROJECT_ROOT / '.env', override=False)
        report_mode = os.environ.get('CITYVERSE_ONPREM_CANONICAL_REPORTS', 'false')
        if report_mode not in ('false', 'true'):
            raise ValueError('CITYVERSE_ONPREM_CANONICAL_REPORTS must be true or false')
        return cls(
            Path(os.environ.get('CITYVERSE_LOCAL_STATE_FILE', PROJECT_ROOT / '.tmp/cityverse-onprem-state.json')),
            Path(os.environ.get('CITYVERSE_ONPREM_UE_STATE_FILE', PROJECT_ROOT / '.tmp/onprem-ue.sqlite3')),
            Path(os.environ.get('CITYVERSE_ONPREM_ROUND_STATE_FILE', PROJECT_ROOT / '.tmp/onprem-rounds.sqlite3')),
            tuple(x.strip() for x in os.environ.get('CITYVERSE_CORS_ORIGINS',
                'http://localhost:5173,http://127.0.0.1:5173').split(',') if x.strip()),
            canonical_reports=report_mode == 'true',
        )
