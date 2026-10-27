class ApiError(Exception):
    def __init__(self, status, code, message, **details):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    def body(self):
        return {"ret": -1, "error": {"code": self.code, "message": str(self),
                "retryable": self.status in (429, 503), **self.details}}
