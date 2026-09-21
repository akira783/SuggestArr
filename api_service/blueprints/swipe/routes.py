"""
Swipe blueprint — AI-picked cards shown one at a time, votes, requests and the
taste profile. Every route acts on the calling account only (``g.current_user``).
"""

from flask import Blueprint, g, jsonify, request

from api_service.auth.limiter import limiter
from api_service.config.logger_manager import LoggerManager
from api_service.exceptions.api_exceptions import LLMNotConfiguredError, LLMValidationError
from api_service.services.swipe.swipe_service import SwipeError, SwipeService

swipe_bp = Blueprint("swipe", __name__)
logger = LoggerManager.get_logger("SwipeRoute")

_LLM_NOT_CONFIGURED = {
    "status": "error",
    "code": "llm_not_configured",
    "message": "Swipe needs an AI provider. Set OPENAI_API_KEY (and optionally "
               "OPENAI_BASE_URL) in the Advanced settings.",
}


def _error(message, status_code, code=None):
    body = {"status": "error", "message": message}
    if code:
        body["code"] = code
    return jsonify(body), status_code


def _handle(exc, action):
    """Map service exceptions to HTTP responses."""
    if isinstance(exc, SwipeError):
        return _error(str(exc), 400, "invalid_input")
    if isinstance(exc, LLMNotConfiguredError):
        return jsonify(_LLM_NOT_CONFIGURED), 400
    if isinstance(exc, LLMValidationError):
        logger.warning("Swipe %s: LLM returned unusable output: %s", action, exc)
        return _error("The AI provider returned an unusable answer. Try again.", 502, "llm_invalid")
    logger.error("Swipe %s failed: %s", action, exc)
    return _error(f"Swipe {action} failed.", 500)


@swipe_bp.route("/status", methods=["GET"])
def swipe_status():
    """Return what the page needs before showing cards (AI configured, calibration...)."""
    try:
        return jsonify({"status": "success", **SwipeService().status(g.current_user)}), 200
    except Exception as exc:
        return _handle(exc, "status")


@swipe_bp.route("/batch", methods=["GET"])
@limiter.limit("90 per minute")
async def swipe_batch():
    """Return the next batch of cards, or ``pending: true`` while it is generated.

    Generation runs in the background so this request never holds the app's single
    request thread; the client asks again every second or two while pending.

    Query parameters:
        media_type (str): 'movie', 'tv' or 'both' (default).
        mood (str): optional free-text wish for this session.
        mode (str): 'auto' (default), 'normal' or 'calibration'.
    """
    try:
        service = SwipeService()
        if not service.llm_configured(g.current_user["id"]):
            return jsonify(_LLM_NOT_CONFIGURED), 400
        result = await service.next_batch(
            g.current_user,
            media_type=request.args.get("media_type", "both"),
            mood=request.args.get("mood"),
            mode=request.args.get("mode", "auto"),
        )
        return jsonify({"status": "success", **result}), 200
    except Exception as exc:
        return _handle(exc, "batch")


@swipe_bp.route("/vote", methods=["POST"])
@limiter.limit("120 per minute")
def swipe_vote():
    """Record a vote on a card.

    Request body:
        card (dict): the card as served by /batch (id and media_type required).
        vote (str): 'like', 'dislike', 'seen_liked' or 'seen_disliked'.
    """
    try:
        data = request.get_json(silent=True) or {}
        result = SwipeService().vote(g.current_user, data.get("card"), data.get("vote"))
        return jsonify({"status": "success", **result}), 200
    except Exception as exc:
        return _handle(exc, "vote")


@swipe_bp.route("/request", methods=["POST"])
@limiter.limit("30 per minute")
async def swipe_request():
    """Request a card through the regular queue; the approval setting applies.

    Request body:
        card (dict): the card as served by /batch.
    """
    try:
        data = request.get_json(silent=True) or {}
        result = await SwipeService().request(g.current_user, data.get("card"))
        return jsonify({"status": "success", **result}), 200
    except Exception as exc:
        return _handle(exc, "request")


@swipe_bp.route("/profile", methods=["GET"])
def swipe_profile_get():
    """Return the caller's taste profile (null before the first one), whether a
    background refresh is running, and the error of the last one if it failed."""
    try:
        return jsonify({"status": "success", **SwipeService().profile_state(g.current_user)}), 200
    except Exception as exc:
        return _handle(exc, "profile")


@swipe_bp.route("/profile", methods=["PUT"])
@limiter.limit("30 per minute")
def swipe_profile_put():
    """Replace the taste profile with text written by the user.

    Request body:
        profile_text (str): the new profile; later AI revisions will not contradict it.
    """
    try:
        data = request.get_json(silent=True) or {}
        text = data.get("profile_text")
        if not isinstance(text, str):
            return _error("profile_text must be a string", 400, "invalid_input")
        profile = SwipeService().save_profile(g.current_user, text[:4000])
        return jsonify({"status": "success", "profile": profile}), 200
    except Exception as exc:
        return _handle(exc, "profile")


@swipe_bp.route("/profile/refresh", methods=["POST"])
@limiter.limit("6 per minute")
def swipe_profile_refresh():
    """Start rewriting the taste profile in the background; poll GET /profile."""
    try:
        service = SwipeService()
        if not service.llm_configured(g.current_user["id"]):
            return jsonify(_LLM_NOT_CONFIGURED), 400
        started = service.start_profile_refresh(g.current_user)
        return jsonify({"status": "success", "refreshing": True, "started": started}), 202
    except Exception as exc:
        return _handle(exc, "profile refresh")


@swipe_bp.route("/stats", methods=["GET"])
def swipe_stats():
    """Return the success indicators (like rate, request rate, per pick type)."""
    try:
        stats = SwipeService().db.get_swipe_stats(int(g.current_user["id"]))
        return jsonify({"status": "success", "stats": stats}), 200
    except Exception as exc:
        return _handle(exc, "stats")


@swipe_bp.route("/votes", methods=["DELETE"])
@limiter.limit("10 per minute")
def swipe_votes_reset():
    """Delete all of the caller's votes (the profile text is kept)."""
    try:
        return jsonify({"status": "success", **SwipeService().reset(g.current_user)}), 200
    except Exception as exc:
        return _handle(exc, "reset")
