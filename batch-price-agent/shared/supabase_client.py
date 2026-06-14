import os
import logging
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)
_supabase = None


def get_supabase():
    global _supabase
    if _supabase is not None:
        return _supabase
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL és SUPABASE_KEY környezeti változók hiányoznak.")
    from supabase import create_client
    _supabase = create_client(url, key)
    log.info("Supabase kliens inicializálva (batch-price-agent)")
    return _supabase
