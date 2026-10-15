import pymysql
class AppError(Exception):
    """A problem with user input that should be shown as-is."""
def user_message(exc):
    if isinstance(exc, AppError): return str(exc)
    if isinstance(exc, pymysql.err.IntegrityError):
        code=exc.args[0] if exc.args else None
        if code==1062:return "That value already exists."
        if code in (1451,1452):return "A linked record is missing or this record is still in use."
    if isinstance(exc, pymysql.err.DataError): return "One of the values is outside the allowed range or format."
    return "The database rejected the change. Check that the selected records exist and try again."
