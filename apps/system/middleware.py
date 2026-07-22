from loguru import logger
import time



class SlowRequestMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start_time = time.time()
        response = self.get_response(request)
        elapsed = time.time() - start_time
        if elapsed > 30:
            logger.warning(f"Slow request: {request.path} took {elapsed:.2f}s")
            response['X-Request-Time'] = f'{elapsed:.2f}s'
        return response