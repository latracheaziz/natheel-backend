"""Import every model so Alembic / metadata see them."""
from app.models.base import Base
from app.models.media import Media
from app.models.oauth_state import OAuthState
from app.models.post import Post
from app.models.publication import Publication
from app.models.publishing_log import PublishingLog
from app.models.scheduled_post import ScheduledPost
from app.models.social_account import SocialAccount

__all__ = ["Base", "Media", "OAuthState", "Post", "Publication", "PublishingLog",
           "ScheduledPost", "SocialAccount"]
