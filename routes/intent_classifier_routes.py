"""
Intent Classifier Configuration Routes
API endpoints for managing per-organization intent classifier settings.
"""

from flask import Blueprint, request, jsonify
from services.intent_classifier_config_service import IntentClassifierConfigService
from utils.jwt_helper import get_user_from_request
from models.models import UserRole
import logging

logger = logging.getLogger(__name__)

intent_classifier_bp = Blueprint('intent_classifier_config', __name__, url_prefix='/api/admin/intent-classifier')

def _get_target_org_id():
    """
    Determine target organization ID based on user role and request.
    - App Admin: Can only manage their own organization (from token)
    - Super Admin: Can manage any organization (from query param or body)
    """
    user = get_user_from_request()
    if not user:
        return None, "Unauthorized - Invalid or missing token"

    user_role = user.get('role')
    user_org_id = user.get('organization_id')
    
    logger.info(f"Checking permissions for role: '{user_role}', org_id: {user_org_id}")

    # Normalize role for comparison (accepts APP_ADMIN, AppAdmin, SUPER_ADMIN, SuperAdmin)
    is_super_admin = user_role in [UserRole.SUPER_ADMIN.value, 'SuperAdmin', 'SUPER_ADMIN']
    is_app_admin = user_role in [UserRole.APP_ADMIN.value, 'AppAdmin', 'APP_ADMIN']

    if is_super_admin:
        # For super admin, try to get org_id from query or body
        # 1. Query param
        target_org_id = request.args.get('organization_id')
        
        # 2. JSON body (if not in query)
        if not target_org_id and request.is_json:
            target_org_id = request.json.get('organization_id')
            
        if not target_org_id:
            # Fallback to user's org if they have one (unlikely for pure super_admin but possible)
            if user_org_id:
                return user_org_id, None
            return None, "Organization ID is required for Super Admin actions"
            
        try:
            return int(target_org_id), None
        except ValueError:
            return None, "Invalid organization ID format"

    elif is_app_admin:
        # App Admin can only manage their own org
        if not user_org_id:
             return None, "User does not belong to an organization"
        return user_org_id, None
    
    else:
        logger.warning(f"Access denied for role: {user_role}")
        return None, f"Insufficient permissions for role: {user_role}"

@intent_classifier_bp.route('/config', methods=['GET'])
def get_config():
    """Get intent classifier configuration for an organization"""
    org_id, error = _get_target_org_id()
    if error:
        return jsonify({"status": "error", "message": error}), 403

    try:
        config = IntentClassifierConfigService.get_config(org_id)
        return jsonify({"status": "success", "data": config}), 200
    except Exception as e:
        logger.error(f"Error fetching intent config: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@intent_classifier_bp.route('/config', methods=['PUT'])
def update_full_config():
    """Full update of intent classifier configuration"""
    org_id, error = _get_target_org_id()
    if error:
        return jsonify({"status": "error", "message": error}), 403

    if not request.is_json:
        return jsonify({"status": "error", "message": "Request body must be JSON"}), 400

    try:
        updated_config = IntentClassifierConfigService.update_config(org_id, request.json)
        
        # Clear cache to force reload of fresh config on next query
        from services.intent_classification_service import IntentClassificationService
        IntentClassificationService.clear_cache(org_id)  # Use int, not str(org_id)
        logger.info(f"Cleared intent classifier cache for org {org_id} after config update")
        
        return jsonify({"status": "success", "data": updated_config, "message": "Configuration updated"}), 200
    except Exception as e:
        logger.error(f"Error updating intent config: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@intent_classifier_bp.route('/update-section', methods=['PATCH'])
def update_section():
    """
    Partial update of intent classifier configuration.
    Example: update just thresholds or just responses.
    """
    org_id, error = _get_target_org_id()
    if error:
        return jsonify({"status": "error", "message": error}), 403

    if not request.is_json:
        return jsonify({"status": "error", "message": "Request body must be JSON"}), 400

    try:
        updated_config = IntentClassifierConfigService.update_config(org_id, request.json)
        
        # Clear cache to force reload of fresh config on next query
        from services.intent_classification_service import IntentClassificationService
        IntentClassificationService.clear_cache(org_id)  # Use int, not str(org_id)
        logger.info(f"Cleared intent classifier cache for org {org_id} after section update")
        
        return jsonify({"status": "success", "data": updated_config, "message": "Configuration updated"}), 200
    except Exception as e:
        logger.error(f"Error updating intent config section: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@intent_classifier_bp.route('/reset', methods=['POST'])
def reset_config():
    """Reset configuration to system defaults"""
    org_id, error = _get_target_org_id()
    if error:
        return jsonify({"status": "error", "message": error}), 403

    try:
        default_config = IntentClassifierConfigService.reset_to_default(org_id)
        
        # Clear cache to force reload of fresh config on next query
        from services.intent_classification_service import IntentClassificationService
        IntentClassificationService.clear_cache(org_id)  # Use int, not str(org_id)
        logger.info(f"Cleared intent classifier cache for org {org_id} after reset")
        
        return jsonify({"status": "success", "data": default_config, "message": "Reset to defaults"}), 200
    except Exception as e:
        logger.error(f"Error resetting intent config: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@intent_classifier_bp.route('/defaults', methods=['GET'])
def get_defaults():
    """Get system default configuration (readonly)"""
    # Any authenticated user can see defaults? Or just admins. Let's stick to admins.
    user = get_user_from_request()
    if not user:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    
    try:
        defaults = IntentClassifierConfigService._load_defaults_from_json()
        return jsonify({"status": "success", "data": defaults}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
