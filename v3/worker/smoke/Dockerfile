# Phase 1 smoke-test image: the smallest thing that proves SPCS can run a real
# browser and write its output back to a Snowflake stage.
#
# Deliberately NOT the accelerator worker. It carries no accelerator code, so a
# failure here is unambiguously a platform/browser problem rather than
# something in our pipeline. The real worker image (worker/Dockerfile) is built
# only after this passes.
#
# The Playwright base image is used rather than a slim Python one because it
# ships Chromium AND the system libraries matched to the Playwright version --
# hand-installing Chromium onto a slim image is the single most common cause of
# "the browser launches locally but dies in the container".
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    LANG=en_US.UTF-8

WORKDIR /opt/smoke

# snowflake-snowpark-python for the stage upload; pillow for the blank check.
# Pinned so a rebuilt image is the same image.
RUN pip install --no-cache-dir \
        snowflake-snowpark-python==1.23.0 \
        snowflake-connector-python==3.12.3 \
        pillow==11.0.0

COPY worker/smoke/smoke_capture.py ./

# Chromium is already installed in the base image; this is a no-op that fails
# loudly at BUILD time if the base ever stops shipping it.
RUN python -c "from playwright.sync_api import sync_playwright; \
    p=sync_playwright().start(); b=p.chromium.launch(args=['--no-sandbox']); \
    print('chromium', b.version); b.close(); p.stop()"

ENTRYPOINT ["python", "smoke_capture.py"]
