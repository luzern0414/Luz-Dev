FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/backend
RUN addgroup --system luz && adduser --system --ingroup luz --home /srv luz

FROM base AS deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

FROM deps AS runtime
COPY . .
# 把框架与每个模块的 code/ 加入 PYTHONPATH（与 conftest.py 一致）
ENV PYTHONPATH=/srv/backend/00-framework/code:/srv/backend/01-identity/code:/srv/backend/02-store/code:/srv/backend/03-catalog/code:/srv/backend/04-cart/code:/srv/backend/05-address/code:/srv/backend/06-geo/code:/srv/backend/07-inventory/code:/srv/backend/08-order/code:/srv/backend/09-wechat-pay/code:/srv/backend/10-after-sales/code:/srv/backend/11-delivery/code:/srv/backend/12-queue/code:/srv/backend/13-reservation/code:/srv/backend/14-membership/code:/srv/backend/15-recharge/code:/srv/backend/16-coupon/code:/srv/backend/17-campaign/code:/srv/backend/18-lottery/code:/srv/backend/19-review/code:/srv/backend/20-notification/code:/srv/backend/21-media/code:/srv/backend/22-admin-rbac/code:/srv/backend/23-app-settings/code:/srv/backend/24-report/code:/srv/backend/25-jobs/code:/srv/backend/26-agent-support/code:/srv/backend
USER luz
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
