# Visual-validation capture worker for Snowpark Container Services.
#
# This image exists for ONE reason: Streamlit in Snowflake cannot run a
# browser, and a real screenshot needs one. Everything else in the validation
# pack already runs inside SiS.
#
# The Playwright base image is used rather than a bare python image because it
# ships Chromium AND its system libraries already matched to the Playwright
# version. Installing Chromium by hand onto a slim image is the single most
# common source of "browser launches locally, dies in the container".
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Deterministic capture: the same viewport, locale, timezone and theme on
    # every run, so a diff between two runs is a real change and not the
    # container's ambient settings drifting.
    TZ=UTC \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

WORKDIR /opt/accelerator

COPY worker/requirements.txt ./worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt \
 && python -m playwright install chromium --with-deps

# The accelerator runtime the worker REUSES -- same modules the SiS app and the
# CLI use. Listed explicitly rather than copying the repo so an unrelated file
# cannot silently become a runtime dependency of the worker.
COPY validation_worker.py validation_jobs.py app_screenshot.py \
     validation_evidence_bridge.py validation_report.py \
     validation_report_dashboard.py headless_render.py \
     engine.py backend.py config.py calc_translator.py findings.py \
     codegen.py profile_superstore.py profile_default.py \
     tableau_parser.py ./

# Never run as root once the browser is installed.
USER pwuser

# The worker is a long-lived poller; SPCS restarts it if it exits.
ENTRYPOINT ["python", "validation_worker.py"]
