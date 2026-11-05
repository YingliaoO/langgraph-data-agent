<template>
  <div class="landing">
    <header class="top-bar">
      <div class="brand" @click="triggerChat">
        <span class="logo">DA</span>
        <div class="text">
          <h1>Multi-Source Data Agent</h1>
          <p>LangGraph-based Enterprise Data Agent</p>
        </div>
      </div>
      <nav class="nav-links">
        <a href="#features">Capabilities</a>
        <a href="#workflow">Workflow</a>
        <a href="#datasets">Data Domain</a>
        <a href="#faq">FAQ</a>
      </nav>
      <button class="cta" type="button" @click="triggerChat">Open Console</button>
    </header>

    <main class="content">
      <section class="hero">
        <div class="headline">
          <div class="headline-badge">
            <span class="spark">✨</span>
            LangGraph · Router · Tool Calling
          </div>
          <h2><span>Enterprise Data Agent</span> Console</h2>
          <p>
            Coordinate structured queries, knowledge retrieval, and graph reasoning through a LangGraph workflow with observable routing, tool calls, and evaluation traces.
          </p>
          <div class="hero-actions">
            <button class="primary" type="button" @click="triggerChat">Launch Data Console</button>
            <button class="secondary" type="button" @click="triggerChat">Upload a Dataset</button>
          </div>
          <ul class="hero-meta">
            <li>
              <div class="icon-circle">SQL</div>
              <div>
                <span class="value">Text2SQL</span>
                <span class="label">Structured analysis</span>
              </div>
            </li>
            <li>
              <div class="icon-circle">RAG</div>
              <div>
                <span class="value">PGVector</span>
                <span class="label">Knowledge retrieval</span>
              </div>
            </li>
            <li>
              <div class="icon-circle">Trace</div>
              <div>
                <span class="value">Agent Trace</span>
                <span class="label">Evaluation visibility</span>
              </div>
            </li>
          </ul>
        </div>
        <div class="hero-card">
          <div class="card-header">
            <span class="badge">Agent workflow</span>
            <span class="status">LangGraph workflow</span>
          </div>
          <div class="card-body">
            <article class="sample">
              <h3><span>📌</span> Example queries</h3>
              <ul>
                <li>使用 MySQL 统计各商品品类销售件数，并按销量降序排列。</li>
                <li>从知识库检索电子产品无质量问题的退货政策。</li>
                <li>查询 Aurora ANC Headphones 由哪个供应商供货。</li>
              </ul>
            </article>
            <div class="divider"></div>
            <article class="pipeline">
              <h3><span>🧭</span> Execution path</h3>
              <ol>
                <li><span class="step">1</span> Router selects a route</li>
                <li><span class="step">2</span> Planner decomposes complex tasks</li>
                <li><span class="step">3</span> Tool Selection dispatches a data tool</li>
                <li><span class="step">4</span> Trace records the run</li>
              </ol>
            </article>
          </div>
          <footer class="card-footer">
            <span>🔄 Tool calling · trace events</span>
            <span>📎 Current demo domain · Synthetic Enterprise Retail</span>
          </footer>
        </div>
      </section>

      <section id="features" class="feature-grid">
        <article class="feature-card">
          <div class="feature-icon">SQL</div>
          <h3>Text2SQL</h3>
          <p>Retrieve MySQL schema, generate and validate SQL, execute the query, and interpret its result.</p>
        </article>
        <article class="feature-card">
          <div class="feature-icon">RAG</div>
          <h3>Knowledge Retrieval</h3>
          <p>Search the PGVector knowledge path, rerank retrieved context, and show sources when returned.</p>
        </article>
        <article class="feature-card">
          <div class="feature-icon">Graph</div>
          <h3>Graph Reasoning</h3>
          <p>Use the Neo4j graph path to follow relationships across enterprise entities.</p>
        </article>
        <article class="feature-card">
          <div class="feature-icon">Trace</div>
          <h3>Agent Trace</h3>
          <p>Inspect route decisions, selected tools, events, and evaluation context for each run.</p>
        </article>
      </section>

      <section id="workflow" class="workflow">
        <h3>LangGraph execution workflow</h3>
        <div class="timeline">
          <div class="node">
            <span class="dot"></span>
            <div>
              <h4>Router</h4>
              <p>Classify the request and choose the appropriate data path.</p>
            </div>
          </div>
          <div class="node">
            <span class="dot"></span>
            <div>
              <h4>Planner and Tool Selection</h4>
              <p>Decompose complex structured or graph tasks, then dispatch the selected data tool.</p>
            </div>
          </div>
          <div class="node">
            <span class="dot"></span>
            <div>
              <h4>Trace and Evaluation</h4>
              <p>Record route decisions and tool events so each run can be inspected and evaluated.</p>
            </div>
          </div>
        </div>
      </section>

      <section id="datasets" class="datasets">
        <h3>Current demo data domain</h3>
        <p>This public snapshot ships synthetic Enterprise Retail data for local demonstrations. Historical Recipe datasets are not distributed.</p>
        <ul>
          <li><strong>Structured data:</strong> retail customers, products, orders, order items, and refunds for Text2SQL.</li>
          <li><strong>Knowledge base:</strong> return policies, refund SOPs, campaign rules, and supplier SLAs through PGVector.</li>
          <li><strong>Entity graph:</strong> retail products, suppliers, categories, and campaigns queried through Neo4j.</li>
        </ul>
      </section>

      <section id="faq" class="faq">
        <h3>Frequently asked questions</h3>
        <details>
          <summary>How do I connect a business workflow?</summary>
          <p>Use the existing chat and upload entry points to explore the console while preserving session handling.</p>
        </details>
        <details>
          <summary>What does the demonstration cover?</summary>
          <p>The current data domain is provided for exploring structured queries, retrieval, and graph reasoning.</p>
        </details>
        <details>
          <summary>What can I inspect after a run?</summary>
          <p>The console surfaces available route, tool, event, and evaluation trace information when returned by the API.</p>
        </details>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
const emit = defineEmits<{ (e: "start-chat"): void }>();

function triggerChat() {
  emit("start-chat");
}
</script>

<style scoped lang="scss">
.landing {
  min-height: 100vh;
  background: linear-gradient(180deg, #fff7ed 0%, #fff 55%);
  color: #3f3d56;
  padding-bottom: 120px;
}

.top-bar {
  position: sticky;
  top: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 18px 6vw;
  background: rgba(255, 247, 237, 0.88);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid rgba(249, 115, 22, 0.15);
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  cursor: pointer;

  .logo {
    font-size: 32px;
  }

  .text {
    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      color: #ea580c;
    }

    p {
      margin: 0;
      font-size: 13px;
      color: #f97316;
      opacity: 0.85;
    }
  }
}

.nav-links {
  display: flex;
  gap: 18px;
  font-size: 14px;

  a {
    color: #9a3412;
    text-decoration: none;
    font-weight: 600;
    position: relative;

    &:after {
      content: "";
      position: absolute;
      left: 0;
      bottom: -6px;
      width: 100%;
      height: 2px;
      background: linear-gradient(135deg, #f97316, #ef4444);
      transform: scaleX(0);
      transform-origin: left;
      transition: transform 0.2s ease;
    }

    &:hover:after {
      transform: scaleX(1);
    }
  }
}

.cta {
  border: none;
  border-radius: 999px;
  padding: 10px 20px;
  background: linear-gradient(135deg, #f97316, #ef4444);
  color: #fff;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 12px 24px rgba(239, 68, 68, 0.25);
  transition: transform 0.2s ease;

  &:hover {
    transform: translateY(-2px);
  }
}

.content {
  max-width: 1100px;
  margin: 0 auto;
  padding: 60px 6vw 0;
  display: flex;
  flex-direction: column;
  gap: 72px;
}

.hero {
  position: relative;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 32px;
  align-items: stretch;
}

.headline {
  display: flex;
  flex-direction: column;
  gap: 24px;

  .headline-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(249, 115, 22, 0.15);
    color: #b45309;
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;

    .spark {
      font-size: 14px;
    }
  }

  h2 {
    margin: 0;
    font-size: clamp(28px, 3.2vw, 42px);
    font-weight: 700;
    color: #1f2937;

    span {
      color: #ea580c;
    }
  }

  p {
    margin: 0;
    font-size: 15px;
    line-height: 1.7;
    color: #4b5563;
  }
}

.hero-actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;

  button {
    border: none;
    padding: 12px 20px;
    border-radius: 12px;
    font-weight: 600;
    cursor: pointer;
  }

  .primary {
    background: linear-gradient(135deg, #f97316, #ef4444);
    color: #fff;
    box-shadow: 0 14px 24px rgba(239, 68, 68, 0.25);
  }

  .secondary {
    background: #fff;
    color: #f97316;
    border: 1px solid rgba(249, 115, 22, 0.3);
  }
}

.hero-meta {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  list-style: none;
  margin: 0;
  padding: 0;

  li {
    background: rgba(255, 247, 237, 0.82);
    border: 1px solid rgba(249, 115, 22, 0.2);
    border-radius: 12px;
    padding: 12px 16px;
    min-width: 140px;
    display: flex;
    align-items: center;
    gap: 10px;

    .icon-circle {
      width: 38px;
      height: 38px;
      border-radius: 14px;
      background: rgba(249, 115, 22, 0.2);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
      color: #b45309;
    }

    .value {
      font-size: 22px;
      font-weight: 700;
      color: #ea580c;
    }

    .label {
      display: block;
      font-size: 12px;
      color: #9a3412;
    }
  }
}

.hero-card {
  background: radial-gradient(circle at top, rgba(255, 255, 255, 0.92), #fff8f1 60%);
  border-radius: 20px;
  border: 1px solid rgba(249, 115, 22, 0.25);
  box-shadow: 0 24px 48px rgba(249, 115, 22, 0.15);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 20px;
  background: linear-gradient(135deg, rgba(249, 115, 22, 0.1), rgba(239, 68, 68, 0.1));
  color: #9a3412;
  font-weight: 600;

  .badge {
    padding: 6px 12px;
    border-radius: 999px;
    background: rgba(249, 115, 22, 0.15);
  }

  .status {
    font-size: 12px;

    &.online::before {
      content: "";
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #22c55e;
      margin-right: 6px;
      box-shadow: 0 0 0 4px rgba(34, 197, 94, 0.15);
    }
  }
}

.card-body {
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.sample,
.pipeline {
  h3 {
    margin: 0 0 10px;
    font-size: 14px;
    color: #9a3412;

    span {
      margin-right: 6px;
    }
  }

  ul,
  ol {
    margin: 0;
    padding-left: 20px;
    font-size: 13px;
    color: #4b5563;
    line-height: 1.6;
  }

  .step {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: rgba(249, 115, 22, 0.2);
    color: #9a3412;
    font-size: 11px;
    margin-right: 6px;
  }
}

.divider {
  height: 1px;
  background: rgba(249, 115, 22, 0.25);
}

.card-footer {
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: #9a3412;
  background: rgba(249, 115, 22, 0.1);
}

.feature-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 18px;
}

.feature-card {
  padding: 20px;
  border-radius: 16px;
  background: #fff;
  border: 1px solid rgba(249, 115, 22, 0.2);
  box-shadow: 0 18px 36px rgba(249, 115, 22, 0.08);
  display: flex;
  flex-direction: column;
  gap: 10px;

  .feature-icon {
    width: 42px;
    height: 42px;
    border-radius: 14px;
    background: rgba(249, 115, 22, 0.18);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    color: #b45309;
  }

  h3 {
    margin: 0 0 8px;
    font-size: 16px;
    color: #9a3412;
  }

  p {
    margin: 0;
    font-size: 13px;
    line-height: 1.6;
    color: #4b5563;
  }
}

.workflow {
  background: #fffdfb;
  border-radius: 20px;
  border: 1px solid rgba(249, 115, 22, 0.2);
  box-shadow: 0 18px 42px rgba(249, 115, 22, 0.1);
  padding: 28px;

  h3 {
    margin: 0 0 18px;
    font-size: 18px;
    color: #9a3412;
  }
}

.timeline {
  display: grid;
  gap: 18px;
}

.node {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 12px;
  align-items: flex-start;
  color: #4b5563;

  .dot {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: radial-gradient(circle at center, #f97316 0%, #ef4444 70%);
    box-shadow: 0 0 0 6px rgba(249, 115, 22, 0.15);
    margin-top: 6px;
  }

  h4 {
    margin: 0 0 6px;
    font-size: 15px;
    color: #9a3412;
  }

  p {
    margin: 0;
    font-size: 13px;
    line-height: 1.6;
  }
}

.datasets {
  background: #fff;
  border-radius: 20px;
  padding: 26px;
  border: 1px solid rgba(249, 115, 22, 0.18);
  box-shadow: 0 16px 36px rgba(249, 115, 22, 0.08);

  h3 {
    margin: 0 0 12px;
    font-size: 18px;
    color: #9a3412;
  }

  ul {
    margin: 0;
    padding-left: 20px;
    color: #4b5563;
    line-height: 1.7;
  }
}

.faq {
  background: #fffdfb;
  border-radius: 20px;
  border: 1px solid rgba(249, 115, 22, 0.18);
  box-shadow: 0 14px 32px rgba(249, 115, 22, 0.08);
  padding: 24px;
  display: grid;
  gap: 12px;

  h3 {
    margin: 0;
    font-size: 18px;
    color: #9a3412;
  }

  details {
    background: rgba(255, 247, 237, 0.6);
    border: 1px solid rgba(249, 115, 22, 0.18);
    border-radius: 12px;
    padding: 12px 16px;
    font-size: 13px;
    color: #4b5563;

    summary {
      cursor: pointer;
      font-weight: 600;
      color: #9a3412;
    }

    p {
      margin: 8px 0 0;
      line-height: 1.6;
    }
  }
}

@media (max-width: 768px) {
  .top-bar {
    flex-wrap: wrap;
    gap: 16px;
  }

  .nav-links {
    width: 100%;
    justify-content: center;
    flex-wrap: wrap;
    gap: 12px;
  }

  .cta {
    width: 100%;
  }

  .hero-card {
    order: -1;
  }
}
</style>
