// Luz API 压测脚本（k6 >= 0.50；CI 使用 grafana/k6:2.3.0 镜像）。复制到项目 infra/loadtest/k6-api.js。
//
// 场景（开放模型，按到达率施压）：
//   browse        浏览列表（读）           读 p95 < 300ms
//   login         登录（写，低频）          写 p95 < 800ms
//   create_order  下单（写，带幂等键）      写 p95 < 800ms
// 全局：错误率 < 0.5%，checks > 99%。
//
// 运行：
//   k6 run -e PROFILE=smoke -e BASE=https://staging-api.example.com k6-api.js        # 发布后冒烟（1 分钟）
//   k6 run -e PROFILE=load  -e BASE=https://staging-api.example.com k6-api.js        # 容量验证（约 20 分钟）
//   k6 run -e PROFILE=soak  -e BASE=https://staging-api.example.com k6-api.js        # 稳定性（2 小时）
// 规则：
//   - 只打预发，绝不打生产；写接口使用测试商户、测试门店与测试 SKU（库存充足），压完清理。
//   - 压测机出口 IP 必须加入 nginx.conf 的 geo $luz_ratelimit_exempt 白名单，并临时调高应用层限流
//     （RATE_IP_PER_SECOND 等），否则测到的是限流（429 会计入失败）而不是服务能力。
//   - 预发的微信登录需走测试桩（code2session 假实现）；LOGIN_BODY 按项目约定构造。
// 环境变量：
//   BASE           目标地址（必填，默认 http://localhost:8000）
//   PROFILE        smoke | load | soak（默认 smoke）
//   BROWSE_PATHS   逗号分隔的只读路径（默认商品列表与公开配置）
//   LOGIN_PATH     默认 /api/v1/app/auth/login
//   LOGIN_BODY     登录请求体模板，{vu} 与 {iter} 会被替换，默认 {"code":"k6-{vu}-{iter}"}
//   TOKEN          直接提供访问令牌（不在 setup 中登录）
//   TEST_USERS     setup 中预登录的用户数（默认 20），下单按 VU 轮流使用
//   ORDER_BODY     下单请求体 JSON（必须按项目接口填写：门店、SKU、数量、地址等）；未设置则跳过下单场景
import http from 'k6/http';
import { check, sleep, fail } from 'k6';

// 幂等键：优先用 k6 内置 WebCrypto（不从网络加载第三方模块，避免供应链风险）
function uuidv4() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = Math.floor(Math.random() * 16);
    return (c === 'x' ? r : ((r & 0x3) | 0x8)).toString(16);
  });
}

const BASE = (__ENV.BASE || 'http://localhost:8000').replace(/\/$/, '');
const PROFILE = __ENV.PROFILE || 'smoke';
const BROWSE_PATHS = (__ENV.BROWSE_PATHS || '/api/v1/app/products?limit=10,/api/v1/app/config')
  .split(',').map((p) => p.trim()).filter(Boolean);
const LOGIN_PATH = __ENV.LOGIN_PATH || '/api/v1/app/auth/login';
const LOGIN_BODY = __ENV.LOGIN_BODY || '{"code":"k6-{vu}-{iter}"}';
const ORDER_BODY = __ENV.ORDER_BODY || '';
const TEST_USERS = parseInt(__ENV.TEST_USERS || '20', 10);

// 每秒到达率（迭代/秒）；load 按“预估峰值 1 倍 → 2 倍”阶梯，找拐点
const PROFILES = {
  smoke: {
    browse: { executor: 'constant-arrival-rate', rate: 2, timeUnit: '1s', duration: '1m', preAllocatedVUs: 2, maxVUs: 10 },
    login: { executor: 'constant-arrival-rate', rate: 6, timeUnit: '1m', duration: '1m', preAllocatedVUs: 1, maxVUs: 2 },
    create_order: { executor: 'constant-arrival-rate', rate: 12, timeUnit: '1m', duration: '1m', preAllocatedVUs: 1, maxVUs: 4 },
  },
  load: {
    browse: {
      executor: 'ramping-arrival-rate', startRate: 10, timeUnit: '1s', preAllocatedVUs: 50, maxVUs: 300,
      stages: [
        { target: 50, duration: '3m' },   // 预估峰值 1 倍
        { target: 50, duration: '10m' },
        { target: 100, duration: '3m' },  // 2 倍峰值
        { target: 100, duration: '3m' },
        { target: 0, duration: '1m' },
      ],
    },
    login: { executor: 'constant-arrival-rate', rate: 2, timeUnit: '1s', duration: '20m', preAllocatedVUs: 5, maxVUs: 30 },
    create_order: {
      executor: 'ramping-arrival-rate', startRate: 1, timeUnit: '1s', preAllocatedVUs: 20, maxVUs: 150,
      stages: [
        { target: 10, duration: '3m' },
        { target: 10, duration: '10m' },
        { target: 20, duration: '3m' },
        { target: 20, duration: '3m' },
        { target: 0, duration: '1m' },
      ],
    },
  },
  soak: {
    browse: { executor: 'constant-arrival-rate', rate: 30, timeUnit: '1s', duration: '2h', preAllocatedVUs: 50, maxVUs: 200 },
    login: { executor: 'constant-arrival-rate', rate: 30, timeUnit: '1m', duration: '2h', preAllocatedVUs: 2, maxVUs: 10 },
    create_order: { executor: 'constant-arrival-rate', rate: 3, timeUnit: '1s', duration: '2h', preAllocatedVUs: 10, maxVUs: 60 },
  },
};

const chosen = PROFILES[PROFILE] || fail(`unknown PROFILE ${PROFILE}`);
const scenarios = {
  browse: { ...chosen.browse, exec: 'browse', tags: { flow: 'browse' } },
  login: { ...chosen.login, exec: 'login', tags: { flow: 'login' } },
};
if (ORDER_BODY) scenarios.create_order = { ...chosen.create_order, exec: 'createOrder', tags: { flow: 'order' } };

export const options = {
  scenarios,
  thresholds: {
    // 同一指标键只能出现一次，否则前面的会被静默丢弃
    http_req_failed: ['rate<0.005'],
    'http_req_duration{kind:read}': ['p(95)<300', 'p(99)<1000'],
    'http_req_duration{kind:write}': ['p(95)<800', 'p(99)<2000'],
    checks: [{ threshold: 'rate>0.99', abortOnFail: true, delayAbortEval: '30s' }],
    dropped_iterations: ['count<100'],   // 达不到目标到达率：压测机或服务已饱和
  },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
  setupTimeout: '2m',
  userAgent: 'luz-k6/1.0',
};

const JSON_HEADERS = { 'Content-Type': 'application/json' };

function loginOnce(vu, iter) {
  const body = LOGIN_BODY.replace('{vu}', String(vu)).replace('{iter}', String(iter));
  const res = http.post(`${BASE}${LOGIN_PATH}`, body, { headers: JSON_HEADERS, tags: { kind: 'write', name: 'login' } });
  const ok = check(res, {
    'login 200': (r) => r.status === 200,
    'login code 0': (r) => { try { return r.json('code') === 0; } catch (e) { return false; } },
  });
  if (!ok) return null;
  return res.json('data.access_token') || res.json('data.token') || null;
}

export function setup() {
  if (__ENV.TOKEN) return { tokens: [__ENV.TOKEN] };
  if (!ORDER_BODY) return { tokens: [] };
  const tokens = [];
  for (let i = 0; i < TEST_USERS; i += 1) {
    const t = loginOnce(`setup${i}`, 0);
    if (t) tokens.push(t);
  }
  if (tokens.length === 0) fail('setup: no test user could log in; check LOGIN_BODY / code2session stub');
  return { tokens };
}

export function browse() {
  const path = BROWSE_PATHS[Math.floor(Math.random() * BROWSE_PATHS.length)];
  const res = http.get(`${BASE}${path}`, { tags: { kind: 'read', name: path.split('?')[0] } });
  check(res, {
    'browse 200': (r) => r.status === 200,
    'browse envelope': (r) => { try { return r.json('code') === 0; } catch (e) { return false; } },
  });
}

export function login() {
  loginOnce(__VU, __ITER);
}

export function createOrder(data) {
  const token = data.tokens[(__VU + __ITER) % data.tokens.length];
  const headers = { ...JSON_HEADERS, Authorization: `Bearer ${token}`, 'Idempotency-Key': uuidv4() };
  const res = http.post(`${BASE}/api/v1/app/orders`, ORDER_BODY, { headers, tags: { kind: 'write', name: 'create_order' } });
  check(res, {
    'order 2xx': (r) => r.status >= 200 && r.status < 300,
    'order code 0': (r) => { try { return r.json('code') === 0; } catch (e) { return false; } },
  });
  if (PROFILE === 'smoke') sleep(1);
}
