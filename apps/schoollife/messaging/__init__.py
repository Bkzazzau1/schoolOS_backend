from .parent_messages import (
    PARENT_MESSAGE,
    PARENT_MESSAGE_RECEIPT,
    ParentMessageHandler,
    ParentMessageReceiptHandler,
)
from .teacher_channels import (
    TEACHER_DEPARTMENT_MESSAGE,
    TEACHER_DEPARTMENT_RECEIPT,
    TEACHER_LEADERSHIP_MESSAGE,
    TEACHER_LEADERSHIP_RECEIPT,
    TeacherDepartmentMessageHandler,
    TeacherDepartmentReceiptHandler,
    TeacherLeadershipMessageHandler,
    TeacherLeadershipReceiptHandler,
)

HANDLERS = [
    ParentMessageHandler(),
    ParentMessageReceiptHandler(),
    TeacherLeadershipMessageHandler(),
    TeacherLeadershipReceiptHandler(),
    TeacherDepartmentMessageHandler(),
    TeacherDepartmentReceiptHandler(),
]
