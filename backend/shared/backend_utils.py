"""
backend_utils.py

Shared utilities for backend modules including error handling decorators,
validation functions, and common constants.
"""

import logging
import inspect
import re
from functools import wraps
from typing import Any, Dict, Tuple, Optional

logger = logging.getLogger(__name__)

# Define recoverable error categories
RECOVERABLE_ERRORS = (
    ConnectionError, TimeoutError,  # Network issues
    IOError, OSError,               # File system issues  
    ValueError, KeyError,           # Data issues
    RuntimeError,                   # Our own errors
)

def standardize_response(func):
    """Decorator to ensure all public functions return consistent error format"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
            # If function returns a dict with success key, pass through
            if isinstance(result, dict) and 'success' in result:
                return result
            # Otherwise wrap the result
            return {"success": True, "result": result}
        except ValueError as e:
            return {"success": False, "error": str(e), "error_type": "VALIDATION"}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e), "error_type": "NOT_FOUND"}
        except RuntimeError as e:
            resp = {"success": False, "error": str(e), "error_type": "RUNTIME"}
            # ag_code規約(STTError/AGError): 消費者(UI)のi18n照合用に追加キーで
            # 引き継ぐ。既存キーは不変=消費者互換(稜裁定 2026-07-25)。
            if getattr(e, "ag_code", None):
                resp["error_code"] = e.ag_code
                resp["error_params"] = getattr(e, "ag_params", None) or {}
            return resp
        except PermissionError as e:
            return {"success": False, "error": str(e), "error_type": "PERMISSION"}
        except (KeyboardInterrupt, SystemExit):
            # Re-raise system-level exceptions
            raise
        except Exception as e:
            if getattr(e, "ag_code", None):
                # コード付きアプリ例外は英語原文+コードのまま返す(潰さない)
                return {"success": False, "error": str(e), "error_type": "APP",
                        "error_code": e.ag_code,
                        "error_params": getattr(e, "ag_params", None) or {}}
            logger.exception(f"Unexpected error in {func.__name__}")
            # User-friendly error message
            error_msg = "An unexpected error occurred"
            if func.__name__:
                error_msg += f" during {func.__name__.replace('_', ' ')}"
            return {"success": False, "error": error_msg, "error_type": "INTERNAL",
                    "details": {"original_error": str(e)}}
    return wrapper


def validate_input(param_validators: dict):
    """Decorator for input validation"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Map args to parameter names
            sig = inspect.signature(func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()
            
            # Validate each parameter
            for param_name, validator in param_validators.items():
                if param_name in bound_args.arguments:
                    value = bound_args.arguments[param_name]
                    is_valid, error_msg = validator(value)
                    if not is_valid:
                        logger.error(f"Validation failed for {param_name}: {error_msg}")
                        raise ValueError(f"Invalid {param_name}: {error_msg}")
            
            return func(*args, **kwargs)
        return wrapper
    return decorator

# Validation functions
def validate_character_id(value: Any) -> Tuple[bool, str]:
    if value is None:
        return True, ""  # Optional
    if not isinstance(value, str):
        return False, "must be a string"
    if not value.strip():
        return False, "cannot be empty"
    if not re.match(r'^[a-zA-Z0-9_-]+$', value):
        return False, "contains invalid characters"
    return True, ""

def validate_dict_param(value: Any) -> Tuple[bool, str]:
    if not isinstance(value, dict):
        return False, "must be a dictionary"
    return True, ""

def validate_bool_param(value: Any) -> Tuple[bool, str]:
    if not isinstance(value, bool):
        return False, "must be a boolean"
    return True, ""

def validate_string_param(value: Any) -> Tuple[bool, str]:
    if not isinstance(value, str):
        return False, "must be a string"
    return True, ""

def validate_int_param(value: Any) -> Tuple[bool, str]:
    if not isinstance(value, int) or isinstance(value, bool):
        return False, "must be an integer"
    if value < 0:
        return False, "must be non-negative"
    return True, ""

def validate_message(msg: Dict[str, Any]) -> bool:
    """Validate a message is not corrupted before saving/restoring"""
    if not isinstance(msg, dict):
        return False
    if 'role' not in msg or 'content' not in msg:
        return False
    if msg['role'] not in ['user', 'assistant', 'system']:
        return False
    if not isinstance(msg['content'], str):
        return False
    # Sanity check on message length
    if len(msg['content']) > 100000:  # 100KB limit per message
        logger.warning(f"Message exceeds size limit: {len(msg['content'])} characters")
        return False
    # Check for null bytes or other corruption indicators
    if '\x00' in msg['content']:
        return False
    return True

def check_resources_cached(resource_cache: Dict[str, Any], cache_ttl: float = 5.0) -> Optional[Dict[str, float]]:
    """Check system resources with caching to reduce overhead
    
    Args:
        resource_cache: Dictionary containing 'time' and 'data' keys
        cache_ttl: Cache time-to-live in seconds
        
    Returns:
        Dictionary with 'memory' and 'cpu' percentages, or None if unavailable
    """
    import time
    
    now = time.time()
    
    # Return cached data if still fresh
    if resource_cache.get('time', 0) + cache_ttl > now and resource_cache.get('data'):
        return resource_cache['data']
        
    try:
        import psutil
        data = {
            'memory': psutil.virtual_memory().percent,
            'cpu': psutil.cpu_percent(interval=0.1),
            'disk_free': check_disk_space()  # Add disk space check
        }
        # Update cache
        resource_cache['time'] = now
        resource_cache['data'] = data
        return data
    except ImportError:
        logger.debug("psutil not available for resource monitoring")
        return None
    except Exception as e:
        logger.warning(f"Failed to check system resources: {e}")
        return None

def check_disk_space(min_free_mb: int = 100) -> Dict[str, Any]:
    """Check available disk space for critical directories
    
    Args:
        min_free_mb: Minimum free space in MB to consider healthy
        
    Returns:
        Dictionary with disk space status and details
    """
    import shutil
    from pathlib import Path
    
    # Import constants
    from backend.shared.constants import MEMORY_DIR, LOGS_DIR, CHARACTER_CONFIGS_DIR
    
    result = {
        'healthy': True,
        'total_free_mb': float('inf'),
        'warnings': []
    }
    
    critical_dirs = [
        ('memory', MEMORY_DIR),
        ('logs', LOGS_DIR),
        ('configs', CHARACTER_CONFIGS_DIR)
    ]
    
    for name, directory in critical_dirs:
        try:
            # Ensure directory exists before checking
            Path(directory).mkdir(parents=True, exist_ok=True)
            
            stat = shutil.disk_usage(directory)
            free_mb = stat.free / (1024 * 1024)
            
            if free_mb < min_free_mb:
                result['healthy'] = False
                result['warnings'].append(f"{name}: only {free_mb:.1f}MB free")
                logger.warning(f"Low disk space in {directory}: {free_mb:.1f}MB free")
            
            # Track minimum free space across all directories
            result['total_free_mb'] = min(result['total_free_mb'], free_mb)
            
            # Check for SQLite databases and their WAL files
            if name == 'memory':  # Only check memory directory for DB files
                for db_file in Path(directory).glob("*.db"):
                    try:
                        wal_file = Path(f"{db_file}-wal")
                        shm_file = Path(f"{db_file}-shm")
                        
                        db_size_mb = db_file.stat().st_size / (1024 * 1024) if db_file.exists() else 0
                        wal_size_mb = wal_file.stat().st_size / (1024 * 1024) if wal_file.exists() else 0
                        shm_size_mb = shm_file.stat().st_size / (1024 * 1024) if shm_file.exists() else 0
                        
                        # Need at least 2.5x the combined size for safe checkpoint
                        total_db_size_mb = db_size_mb + wal_size_mb + shm_size_mb
                        required_mb = total_db_size_mb * 2.5
                        
                        if free_mb < required_mb:
                            result['healthy'] = False
                            warning_msg = (f"Low space for {db_file.name} checkpoint: "
                                         f"need {required_mb:.1f}MB (2.5x of {total_db_size_mb:.1f}MB), "
                                         f"have {free_mb:.1f}MB")
                            result['warnings'].append(warning_msg)
                            logger.warning(warning_msg)
                            
                            # If WAL is particularly large, warn about it
                            if wal_size_mb > db_size_mb:
                                wal_warning = f"WAL file for {db_file.name} is larger than DB ({wal_size_mb:.1f}MB > {db_size_mb:.1f}MB)"
                                result['warnings'].append(wal_warning)
                                logger.warning(wal_warning)
                                
                    except Exception as db_check_error:
                        logger.debug(f"Error checking database files: {db_check_error}")
            
        except Exception as e:
            logger.error(f"Failed to check disk space for {directory}: {e}")
            result['warnings'].append(f"{name}: check failed")
    
    # Critical warning if very low
    if result['total_free_mb'] < 50:
        logger.critical(f"CRITICAL: Only {result['total_free_mb']:.1f}MB free disk space!")
    
    return result

def sanitize_user_input(text: str) -> str:
    """Remove potential prompt injection patterns to protect character personality
    
    Args:
        text: User input text to sanitize
        
    Returns:
        Sanitized text with injection patterns removed
    """
    if not text:
        return text
    
    # Patterns that could be used for prompt injection
    injection_patterns = [
        # System-like instructions
        (r'\[SYSTEM\]', ''),
        (r'\[system\]', ''),
        (r'\[INST\]', ''),
        (r'\[/INST\]', ''),
        (r'</s>', ''),
        (r'<s>', ''),
        (r'<\|im_start\|>', ''),
        (r'<\|im_end\|>', ''),
        
        # Common prompt markers
        (r'###\s*Instruction:', ''),
        (r'###\s*System:', ''),
        (r'###\s*Human:', ''),
        (r'###\s*Assistant:', ''),
        
        # Role switching attempts. Anchored to line start (^ with re.MULTILINE)
        # and neutralized by quoting instead of deleted: deletion also ate a
        # legitimate message that merely BEGINS with the phrase (e.g. "From now
        # on, let's speak English.") — M25. Quoting keeps the user's words while
        # marking the sentence as reported speech rather than an instruction.
        (r'^\s*(You are now.*?\.)', r'"\1"'),
        (r'^\s*(Ignore previous instructions.*?\.)', r'"\1"'),
        (r'^\s*(Forget everything.*?\.)', r'"\1"'),
        (r'^\s*(From now on.*?\.)', r'"\1"'),
        
        # Command-like patterns
        (r'^/system\s+', ''),
        (r'^/debug\s+', ''),
        (r'^/admin\s+', ''),
    ]
    
    sanitized = text
    
    # Apply all sanitization patterns
    for pattern, replacement in injection_patterns:
        try:
            sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            logger.warning(f"Regex error in sanitization pattern {pattern}: {e}")
            continue
    
    # Remove multiple consecutive newlines that could be used to separate instructions
    sanitized = re.sub(r'\n{3,}', '\n\n', sanitized)
    
    # Trim the result
    sanitized = sanitized.strip()
    
    # Log if we changed anything
    if sanitized != text:
        logger.warning(f"Sanitized user input: neutralized potential injection ({len(text)} -> {len(sanitized)} chars)")
    
    return sanitized