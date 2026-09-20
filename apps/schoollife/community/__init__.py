from .comments import CommentHandler
from .posts import PostHandler
from .reactions import ReactionHandler
from .reports import ReportHandler

HANDLERS = [PostHandler(), CommentHandler(), ReactionHandler(), ReportHandler()]
