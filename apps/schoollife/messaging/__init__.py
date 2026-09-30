from .parent_messages import (
    PARENT_MESSAGE,
    PARENT_MESSAGE_RECEIPT,
    ParentMessageHandler,
    ParentMessageReceiptHandler,
)

HANDLERS = [ParentMessageHandler(), ParentMessageReceiptHandler()]
