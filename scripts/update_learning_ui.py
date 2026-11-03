with open("app/templates/dashboard.html", "r", encoding="utf-8") as f:
    text = f.read()

# Update loadPerformanceDNA
old_perf_fn_start = "    async function loadPerformanceDNA(showToastFeedback = false) {"
old_perf_fn_end = "    // ===== QUALITY & FACT-CHECK MODAL ====="

new_learning_dna_js = """    async function loadPerformanceDNA(showToastFeedback = false) {
      const container = document.getElementById('performanceContainer');
      container.innerHTML = '<div class="empty-state"><span class="spinner"></span> Loading Editorial Learning Engine & Feedback Analytics...</div>';
      try {
        const [summaryRes, recsRes, perfRes] = await Promise.all([
          fetch('/api/editorial/feedback/summary', { headers: getAuthHeaders() }),
          fetch('/api/editorial/learning/recommendations', { headers: getAuthHeaders() }),
          fetch('/api/analytics/performance/insights?days=30', { headers: getAuthHeaders() }),
        ]);

        const summary = summaryRes.ok ? await summaryRes.json() : {};
        const recsData = recsRes.ok ? await recsRes.json() : { recommendations: [] };
        const perf = perfRes.ok ? await perfRes.json() : {};

        const recs = recsData.recommendations || [];
        const isInsufficient = summary.insufficient_data;
        const totalReviewed = summary.total_reviewed || 0;

        container.innerHTML = `
          <!-- Insufficient Data Warning Banner -->
          ${isInsufficient ? `
          <div style="background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.3); border-radius: var(--radius-md); padding: 14px 18px; margin-bottom: 20px; display: flex; align-items: center; justify-content: space-between;">
            <div>
              <div style="font-weight: 700; color: #fbbf24; font-size: 13.5px; margin-bottom: 3px;">
                ⚠ Insufficient Operator Feedback Cohort (${totalReviewed} / ${summary.min_sample_threshold || 10} reviews)
              </div>
              <div style="font-size: 12px; color: var(--text-muted);">
                The Editorial Learning Engine requires at least 10 real human decisions before drawing confident statistical conclusions or auto-proposing persona updates. Continue reviewing drafts in the queue.
              </div>
            </div>
            <span class="badge" style="background: rgba(245,158,11,0.2); color: #fbbf24; font-weight: 700;">OBSERVING</span>
          </div>` : ''}

          <!-- Feedback Summary Metrics -->
          <div class="stats-grid" style="margin-bottom: 20px;">
            <div class="stat-card">
              <div class="stat-label">Reviewed by Operator</div>
              <div class="stat-val" style="color: #60a5fa;">${totalReviewed}</div>
              <div style="font-size: 11px; color: var(--text-dim); margin-top: 2px;">Total decisions recorded</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">Approval Rate</div>
              <div class="stat-val" style="color: #34d399;">${Math.round((summary.approval_rate || 0) * 100)}%</div>
              <div style="font-size: 11px; color: var(--text-dim); margin-top: 2px;">${summary.approval_count || 0} approved candidates</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">Rejections</div>
              <div class="stat-val" style="color: #f87171;">${summary.rejection_count || 0}</div>
              <div style="font-size: 11px; color: var(--text-dim); margin-top: 2px;">Filtered by operator</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">Human In-Place Edits</div>
              <div class="stat-val" style="color: #a78bfa;">${summary.edit_count || 0}</div>
              <div style="font-size: 11px; color: var(--text-dim); margin-top: 2px;">Avg reduction: ${summary.avg_edit_reduction_pct || 0}%</div>
            </div>
          </div>

          <!-- Cohort Distributions -->
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px;">
            <div class="card" style="margin-bottom: 0;">
              <div style="font-size: 13.5px; font-weight: 700; color: #fff; margin-bottom: 12px;">Rejection Reasons Breakdown</div>
              <div style="display: flex; flex-direction: column; gap: 8px;">
                ${Object.entries(summary.rejection_reasons_distribution || {}).map(([reason, cnt]) => `
                  <div style="display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; background: rgba(0,0,0,0.2); border-radius: 4px; font-size: 12px;">
                    <span style="color: var(--text);">${reason.replace(/_/g, ' ')}</span>
                    <span class="badge ${cnt > 0 ? 'badge-danger' : ''}" style="${cnt === 0 ? 'background: rgba(255,255,255,0.05); color: var(--text-dim);' : ''}">${cnt}</span>
                  </div>
                `).join('')}
              </div>
            </div>

            <div class="card" style="margin-bottom: 0;">
              <div style="font-size: 13.5px; font-weight: 700; color: #fff; margin-bottom: 12px;">Critic Score Correlation & Angle Trends</div>
              <div style="display: flex; flex-direction: column; gap: 10px; font-size: 12px;">
                <div style="padding: 10px; background: rgba(0,0,0,0.2); border-radius: 4px;">
                  <div style="color: var(--text-muted); margin-bottom: 2px;">Avg Critic Score (Approved Drafts):</div>
                  <strong style="color: #34d399; font-size: 16px;">${summary.avg_critic_score_approved || 0} / 100</strong>
                </div>
                <div style="padding: 10px; background: rgba(0,0,0,0.2); border-radius: 4px;">
                  <div style="color: var(--text-muted); margin-bottom: 2px;">Avg Critic Score (Rejected Drafts):</div>
                  <strong style="color: #f87171; font-size: 16px;">${summary.avg_critic_score_rejected || 0} / 100</strong>
                </div>
                <div style="padding: 10px; background: rgba(0,0,0,0.2); border-radius: 4px;">
                  <div style="color: var(--text-muted); margin-bottom: 4px;">Common Human Editing Patterns:</div>
                  <div style="color: var(--text);">${(summary.common_editing_patterns || []).length > 0 ? summary.common_editing_patterns.join(', ') : 'None detected yet'}</div>
                </div>
              </div>
            </div>
          </div>

          <!-- Proposed Editorial Learning Recommendations -->
          <div class="card" style="margin-bottom: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
              <div>
                <div style="font-size: 14px; font-weight: 700; color: #fff;">Editorial Learning Engine: Proposed Recommendations</div>
                <div style="font-size: 11.5px; color: var(--text-muted);">Proposals generated from observed human edits and rejection trends. Changes NEVER auto-apply without operator authorization.</div>
              </div>
              <button class="btn btn-secondary btn-sm" onclick="loadPerformanceDNA(true)">Scan For Patterns</button>
            </div>

            ${recs.length === 0 ? `
              <div style="padding: 16px; background: rgba(0,0,0,0.2); border-radius: 6px; text-align: center; color: var(--text-dim); font-size: 12.5px;">
                No pending recommendations. As you reject drafts with specific reasons or perform edits in the Review Queue, pattern recommendations will appear here for approval.
              </div>
            ` : `
              <div style="display: flex; flex-direction: column; gap: 12px;">
                ${recs.map(r => `
                  <div style="padding: 14px; background: rgba(0,0,0,0.25); border-radius: var(--radius-sm); border-left: 3px solid ${r.status === 'applied' ? '#34d399' : (r.status === 'rejected' ? '#f87171' : '#6366f1')};">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                      <div>
                        <strong style="color: #fff; font-size: 13px;">${escapeHtml(r.pattern_type.replace(/_/g, ' '))}</strong>
                        <span class="badge" style="margin-left: 8px; text-transform: uppercase;">${escapeHtml(r.status)}</span>
                      </div>
                      <span class="badge" style="background: rgba(99,102,241,0.15); color: #a5b4fc;">${Math.round(r.confidence * 100)}% Confidence</span>
                    </div>
                    <p style="font-size: 12px; color: var(--text); line-height: 1.45; margin-bottom: 8px;">${escapeHtml(r.evidence_summary)}</p>
                    <div style="font-size: 11.5px; color: var(--text-muted); background: rgba(0,0,0,0.2); padding: 8px; border-radius: 4px; margin-bottom: 10px; font-family: monospace;">
                      Adjustment: ${escapeHtml(JSON.stringify(r.suggested_dna_adjustment))}
                    </div>
                    ${r.status === 'proposed' ? `
                      <div style="display: flex; gap: 8px;">
                        <button class="btn btn-success btn-sm" style="background: #059669; color: #fff; font-size: 11px;" onclick="reviewRecommendation(${r.id}, 'approve')">Approve & Apply to Persona DNA</button>
                        <button class="btn btn-secondary btn-sm" style="font-size: 11px;" onclick="reviewRecommendation(${r.id}, 'reject')">Reject Proposal</button>
                      </div>
                    ` : ''}
                  </div>
                `).join('')}
              </div>
            `}
          </div>

          <!-- Persona DNA Version History & Rollback -->
          <div class="card">
            <div style="font-size: 14px; font-weight: 700; color: #fff; margin-bottom: 6px;">Editorial Persona DNA Versioning & Safe Rollback</div>
            <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 14px;">Every approved adjustment creates an immutable version snapshot. Restore any prior version instantly.</div>
            <div style="display: flex; align-items: center; gap: 10px;">
              <input type="number" id="rollbackTargetVersion" placeholder="Target Version (e.g. 1)" style="width: 180px; background: rgba(0,0,0,0.3); border: 1px solid var(--border); border-radius: var(--radius-sm); color: #fff; padding: 6px 10px; font-size: 12.5px;" min="1">
              <button class="btn btn-secondary btn-sm" onclick="rollbackPersonaDna()">Rollback to Version</button>
            </div>
          </div>
        `;
        if (showFeedback) showToast('Performance and editorial learning updated', 'success');
      } catch (err) {
        container.innerHTML = `<div class="empty-state" style="color: var(--danger-text);">Error loading learning engine: ${err.message}</div>`;
      }
    }

    async function reviewRecommendation(recId, action) {
      try {
        const res = await fetch(`/api/editorial/learning/recommendations/${recId}/review`, {
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({ action })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message || 'Recommendation updated', 'success');
          loadPerformanceDNA();
        } else {
          showToast('Failed: ' + (data.detail || 'error'), 'error');
        }
      } catch (err) {
        showToast('Error: ' + err.message, 'error');
      }
    }

    async function rollbackPersonaDna() {
      const v = parseInt(document.getElementById('rollbackTargetVersion').value);
      if (!v || v < 1) { showToast('Provide a valid version number', 'error'); return; }
      if (!confirm(`Are you sure you want to roll back persona to version ${v}?`)) return;
      try {
        const res = await fetch('/api/editorial/dna/rollback', {
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({ target_version: v })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(data.message || 'Rollback successful', 'success');
          loadPerformanceDNA();
        } else {
          showToast('Rollback failed: ' + (data.detail || 'error'), 'error');
        }
      } catch (err) {
        showToast('Error: ' + err.message, 'error');
      }
    }
"""

idx1 = text.find(old_perf_fn_start)
idx2 = text.find(old_perf_fn_end)
if idx1 != -1 and idx2 != -1:
    text = text[:idx1] + new_learning_dna_js + "\n\n    " + text[idx2:]
    print("REPLACED LOADPERFORMANCEDNA")
else:
    print(f"FAILED TO FIND INDICES: {idx1}, {idx2}")

with open("app/templates/dashboard.html", "w", encoding="utf-8") as f:
    f.write(text)
