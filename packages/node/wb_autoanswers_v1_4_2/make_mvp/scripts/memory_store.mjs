export class MemoryStore {
  constructor() {
    this.jobs = new Map();
    this.caseCodes = [];
    this.audit = new Map();
    this.recentReplies = [];
  }

  async getJob(idempotencyKey) {
    const job = this.jobs.get(idempotencyKey);
    return job ? structuredClone(job) : null;
  }

  async putJob(idempotencyKey, job) {
    this.jobs.set(idempotencyKey, structuredClone(job));
  }

  async listCaseCodes() {
    return structuredClone(this.caseCodes);
  }

  async reserveCaseCode(record) {
    const prior = this.caseCodes.find((item) => item.idempotency_key === record.idempotency_key);
    if (prior) return structuredClone(prior);
    if (this.caseCodes.some((item) => item.case_code === record.case_code && item.active !== false)) {
      throw new Error(`CASE_CODE_COLLISION:${record.case_code}`);
    }
    this.caseCodes.push(structuredClone(record));
    return structuredClone(record);
  }

  async appendAudit(idempotencyKey, event) {
    const events = this.audit.get(idempotencyKey) || [];
    events.push(structuredClone(event));
    this.audit.set(idempotencyKey, events);
  }

  async getAudit(idempotencyKey) {
    return structuredClone(this.audit.get(idempotencyKey) || []);
  }

  async rememberReply(record) {
    this.recentReplies.push(structuredClone(record));
  }
}
