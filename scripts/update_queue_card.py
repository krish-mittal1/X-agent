with open("app/templates/dashboard.html", "r", encoding="utf-8") as f:
    text = f.read()

# Replace saveDraftEdit
old_save_edit = """    async function saveDraftEdit(id) {
      const editEl = document.getElementById(`edit_body_${id}`);
      const viewEl = document.getElementById(`view_body_${id}`);
      const btn = document.getElementById(`btn_edit_${id}`);
      const newText = editEl.value.trim();
      btn.disabled = true;
      try {
        const res = await fetch(`/api/dashboard/drafts/${id}`, {
          method: 'PUT', headers: getAuthHeaders(), body: JSON.stringify({ body: newText })
        });
        if (res.ok) {
          viewEl.innerText = newText;
          editEl.style.display = 'none';
          viewEl.style.display = 'block';
          btn.innerText = 'Edit';
          btn.className = 'btn btn-secondary btn-sm';
          showToast('Draft updated', 'success');
        } else {
          showToast('Failed to save changes', 'error');
        }
      } catch (err) {
        showToast('Save error: ' + err.message, 'error');
      } finally {
        btn.disabled = false;
      }
    }"""

new_save_edit = """    async function saveDraftEdit(id) {
      const editEl = document.getElementById(`edit_body_${id}`);
      const viewEl = document.getElementById(`view_body_${id}`);
      const errEl = document.getElementById(`edit_error_${id}`);
      const btn = document.getElementById(`btn_edit_${id}`);
      const newText = editEl.value.trim();
      if (errEl) errEl.style.display = 'none';
      btn.disabled = true;
      try {
        const res = await fetch(`/api/editorial/feedback/${id}/edit`, {
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({ edited_text: newText, operator_comment: 'Operator in-place edit' })
        });
        if (res.ok) {
          viewEl.innerText = newText;
          editEl.style.display = 'none';
          viewEl.style.display = 'block';
          btn.innerText = 'Edit';
          btn.className = 'btn btn-secondary btn-sm';
          const badge = document.getElementById(`edited_badge_${id}`);
          if (badge) badge.style.display = 'inline-flex';
          showToast('Draft edited and verified by ClaimVerifier', 'success');
        } else {
          const err = await res.json().catch(() => ({}));
          const msg = err.detail || 'Validation failed';
          if (errEl) {
            errEl.innerText = 'Validation Error: ' + msg;
            errEl.style.display = 'block';
          }
          showToast('Edit rejected: ' + msg, 'error');
        }
      } catch (err) {
        showToast('Save error: ' + err.message, 'error');
      } finally {
        btn.disabled = false;
      }
    }"""

if old_save_edit in text:
    text = text.replace(old_save_edit, new_save_edit)
    print("REPLACED saveDraftEdit")
else:
    print("old_save_edit not found")

old_card_start = '          return `\n          <div class="tweet-card" id="card_${d.id}">'
old_card_end = "          `;\n        }).join('');"

new_card_html = """          return `
          <div class="tweet-card" id="card_${d.id}">
            <div class="tweet-header">
              <div class="tweet-author">
                <div class="avatar-sm">SB</div>
                <div>
                  <div class="author-name">NotANerd</div>
                  <div class="author-handle">@ssh_bugs · ${timeStr || 'staged'}</div>
                </div>
              </div>
              <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                <span class="badge" style="background: rgba(99,102,241,0.18); color: #a5b4fc; border: 1px solid rgba(99,102,241,0.3); text-transform: capitalize;">Angle: ${escapeHtml(d.tone || d.format || 'analytical')}</span>
                ${d.quality_score ? `<span class="badge" style="background: rgba(16,185,129,0.12); color: #34d399; border: 1px solid rgba(16,185,129,0.25);">Score: ${Math.round(d.quality_score)}/100</span>` : ''}
                <span class="badge" style="background: rgba(59,130,246,0.12); color: #60a5fa; border: 1px solid rgba(59,130,246,0.25);">✓ Verified (${d.sources_count || 1} sources)</span>
                <span class="badge badge-success" id="edited_badge_${d.id}" style="${(d.review_notes && d.review_notes.includes('Operator')) ? 'display: inline-flex;' : 'display: none;'}">Operator Edited</span>
                <button class="btn btn-secondary btn-sm" onclick="toggleAnglesView(${d.id})" style="padding: 2px 8px; font-size: 11px; color: #a5b4fc; border-color: rgba(99,102,241,0.35);">
                  Compare Angles (${(d.alternative_angles || []).length || 3})
                </button>
                <button class="btn btn-secondary btn-sm" onclick="openQualityModal(${d.id})" style="padding: 2px 8px; font-size: 11px; display: inline-flex; align-items: center; gap: 4px; border-color: rgba(16,185,129,0.3); color: #34d399; background: rgba(16,185,129,0.1);">
                  ★ Quality & Claims
                </button>
              </div>
            </div>

            <div id="edit_error_${d.id}" style="display: none; background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.3); border-radius: 6px; padding: 8px 12px; margin-bottom: 8px; font-size: 12px; color: #f87171;"></div>

            <div id="view_body_${d.id}" class="tweet-body">${escapeHtml(d.body)}</div>
            <textarea id="edit_body_${d.id}" class="tweet-textarea" style="display: none;" oninput="updateCharCount('edit_body_${d.id}', 'count_${d.id}')">${escapeHtml(d.body)}</textarea>

            <!-- Collapsible 3-Angle Drawer -->
            ${(d.alternative_angles && d.alternative_angles.length > 0) ? `
            <div id="angles_box_${d.id}" style="display: none; background: rgba(0,0,0,0.35); border: 1px solid var(--border-subtle); border-radius: 6px; padding: 12px; margin: 10px 0;">
              <div style="font-size: 12px; font-weight: 700; color: #fff; margin-bottom: 8px;">3-Angle Editorial Comparison:</div>
              <div style="display: flex; flex-direction: column; gap: 8px;">
                ${d.alternative_angles.map(alt => `
                  <div style="background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 6px; padding: 10px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                      <span class="badge badge-primary" style="text-transform: capitalize;">${escapeHtml(alt.angle || 'angle')}</span>
                      <button class="btn btn-primary btn-sm" style="font-size: 11px; padding: 2px 8px;" onclick="switchDraftAngle(${d.id}, '${alt.angle}')">Select ${escapeHtml(alt.angle)}</button>
                    </div>
                    <div style="font-size: 12px; color: var(--text); line-height: 1.45;">${escapeHtml((alt.parts || []).join(' ') || alt.hook || '')}</div>
                  </div>
                `).join('')}
              </div>
            </div>` : ''}

            ${d.image_url ? `
            <div id="draft_img_wrap_${d.id}" style="margin: 10px 0; position: relative; border-radius: var(--radius-md); overflow: hidden; border: 1px solid var(--border); max-width: 480px; background: rgba(0,0,0,0.35);">
              <img src="${escapeHtml(d.image_url)}" alt="Post image" style="width: 100%; max-height: 240px; object-fit: cover; display: block;" onerror="this.parentElement.style.display='none'">
              <div style="position: absolute; top: 6px; right: 6px;">
                <button class="btn btn-secondary btn-sm" style="background: rgba(15,19,28,0.85); backdrop-filter: blur(4px); padding: 2px 7px; font-size: 10.5px;" onclick="removeDraftImage(${d.id})">✕ Remove Image</button>
              </div>
              <div style="font-size: 10.5px; color: var(--text-dim); padding: 4px 8px; background: rgba(15,19,28,0.7); border-top: 1px solid var(--border-subtle);">Verified editorial photo</div>
            </div>
            ` : `
            <div id="draft_img_add_${d.id}" style="margin: 6px 0 8px 0;">
              <button class="btn btn-secondary btn-sm" onclick="promptDraftImageUrl(${d.id})" style="font-size: 10.5px; padding: 2px 7px; color: var(--text-dim);">+ Add Verified Image URL</button>
            </div>
            `}

            <div id="ai_box_${d.id}" class="ai-bar" style="display: none;">
              <input type="text" id="ai_input_${d.id}" class="ai-input" placeholder="Prompt AI what to change..." onkeydown="if(event.key==='Enter')refineDraftWithAi(${d.id})">
              <button class="btn btn-primary btn-sm" id="btn_ai_submit_${d.id}" onclick="refineDraftWithAi(${d.id})">Rewrite</button>
              <button class="btn btn-secondary btn-sm" onclick="closeAiBox(${d.id})">Cancel</button>
            </div>

            <div class="tweet-footer">
              <span id="count_${d.id}" class="char-meter">${(d.body || '').length} / 280</span>
              <div class="tweet-actions">
                <button class="btn btn-success btn-sm" onclick="approveDraft(${d.id}, '${d.tone || 'analytical'}')" style="background: #059669; color: #fff; border-color: #10b981; font-weight: 600;">✓ Approve</button>
                <button class="btn btn-danger btn-sm" onclick="openRejectModal(${d.id})" style="background: #dc2626; color: #fff; border-color: #ef4444; font-weight: 600;">✕ Reject</button>
                <button class="btn btn-secondary btn-sm" id="btn_edit_${d.id}" onclick="toggleEditDraft(${d.id})">Edit</button>
                <button class="btn btn-secondary btn-sm" onclick="toggleAnglesView(${d.id})" style="color: #a5b4fc; border-color: rgba(99,102,241,0.35);">Angles</button>
                <button class="btn btn-secondary btn-sm" onclick="openQualityModal(${d.id})">Quality Breakdown</button>
                <button class="btn btn-secondary btn-sm" onclick="copyDraftText(${d.id})">Copy</button>
                <button class="btn btn-secondary btn-sm" onclick="openXWithDraft(${d.id})">Open in X</button>
                <button class="btn btn-primary btn-sm" id="btn_publish_${d.id}" onclick="publishDraftDirect(${d.id})" style="background: #2563eb; color: #fff; border-color: #3b82f6; font-weight: 600;">Publish to X</button>
              </div>
            </div>
            ${d.rationale ? `<p class="tweet-rationale">${escapeHtml(d.rationale)}</p>` : ''}
          </div>"""

idx1 = text.find(old_card_start)
idx2 = text.find(old_card_end)
if idx1 != -1 and idx2 != -1:
    text = text[:idx1] + new_card_html + text[idx2:]
    print("REPLACED CARD HTML")
else:
    print(f"FAILED TO FIND CARD INDICES: {idx1}, {idx2}")

with open("app/templates/dashboard.html", "w", encoding="utf-8") as f:
    f.write(text)
