with open("app/templates/dashboard.html", "r", encoding="utf-8") as f:
    text = f.read()

# Let's check if rejectDraft is defined and replace it with approveDraft and rejection modal handlers
old_reject = """    async function rejectDraft(id) {
      if (!confirm('Discard this draft from review queue?')) return;
      try {
        const res = await fetch(`/drafts/${id}/reject`, {
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({ reviewer: 'admin', notes: 'Discarded by operator' })
        });
        if (res.ok) {
          showToast('Draft discarded from queue', 'info');
          const card = document.getElementById(`card_${id}`);
          if (card) card.remove();
          loadStats();
          loadDrafts();
        } else {
          const err = await res.json().catch(() => ({}));
          showToast('Failed to discard: ' + (err.detail || 'error'), 'error');
        }
      } catch (err) {
        showToast('Failed to reject: ' + err.message, 'error');
      }
    }"""

new_feedback_js = """    async function approveDraft(id, angle) {
      try {
        const res = await fetch('/api/editorial/feedback', {
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({
            draft_id: id,
            action: 'approved',
            candidate_angle: angle || 'single_tweet',
            candidate_version: 1,
            operator_comment: 'Approved by operator for staging'
          })
        });
        if (res.ok) {
          showToast('Draft approved and staged locally', 'success');
          loadDrafts();
          loadStats();
        } else {
          const err = await res.json().catch(() => ({}));
          showToast('Approval failed: ' + (err.detail || 'error'), 'error');
        }
      } catch (err) {
        showToast('Approval error: ' + err.message, 'error');
      }
    }

    function openRejectModal(id) {
      document.getElementById('rejectDraftId').value = id;
      document.getElementById('rejectionCommentInput').value = '';
      document.getElementById('rejectionModal').style.display = 'flex';
    }

    function closeRejectionModal() {
      document.getElementById('rejectionModal').style.display = 'none';
    }

    async function submitDraftRejection() {
      const id = parseInt(document.getElementById('rejectDraftId').value);
      const reason = document.getElementById('rejectionReasonSelect').value;
      const comment = document.getElementById('rejectionCommentInput').value.trim();
      const btn = document.getElementById('btnConfirmReject');
      btn.disabled = true;
      try {
        const res = await fetch('/api/editorial/feedback', {
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({
            draft_id: id,
            action: 'rejected',
            rejection_reason: reason,
            operator_comment: comment
          })
        });
        if (res.ok) {
          showToast(`Draft rejected [${reason}]`, 'info');
          closeRejectionModal();
          const card = document.getElementById(`card_${id}`);
          if (card) card.remove();
          loadDrafts();
          loadStats();
        } else {
          const err = await res.json().catch(() => ({}));
          showToast('Rejection failed: ' + (err.detail || 'error'), 'error');
        }
      } catch (err) {
        showToast('Rejection error: ' + err.message, 'error');
      } finally {
        btn.disabled = false;
      }
    }

    function toggleAnglesView(id) {
      const el = document.getElementById(`angles_box_${id}`);
      if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
    }

    async function rejectDraft(id) {
      openRejectModal(id);
    }"""

if old_reject in text:
    text = text.replace(old_reject, new_feedback_js)
    print("REPLACED REJECT WITH APPROVE + REJECT MODAL")
else:
    print("old_reject not found exactly")

with open("app/templates/dashboard.html", "w", encoding="utf-8") as f:
    f.write(text)
