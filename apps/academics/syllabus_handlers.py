# Superseded. Syllabus progress is now a server-derived, read-only view owned
# by apps.lesson_delivery (see apps/lesson_delivery/handlers.py's
# SyllabusProgressHandler, which is generated from canonical LessonDelivery
# records rather than a Teacher-reported status).
#
# This module's TeacherSyllabusProgressHandler/TeacherSyllabusProgressEventHandler
# were an earlier, pre-canonical design (a Teacher directly writing a
# reportedStatus) that was left registered alongside the newer implementation,
# which meant apps/academics/apps.py and apps/lesson_delivery/apps.py both
# tried to register a handler for the entity type "teacher_syllabus_progress"
# at Django startup - the registry rejects a second registration for the same
# entity type, so this crashed every `manage.py` invocation. apps/academics/apps.py
# no longer imports or registers anything from this file. Nothing else in the
# codebase references it (confirmed by search), so it is safe to delete this
# file entirely - it is kept only as a stub because this session's sandbox
# would not allow deleting it outright.
