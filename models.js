// ── Model management: pull, refresh, delete ──
let pullAbort = null;

async function startPull() {
    var n = document.getElementById("pull-model-name").value.trim();
    if (!n || pullAbort) return;
    document.getElementById("pull-btn").style.display = "none";
    document.getElementById("cancel-btn").style.display = "inline-block";
    document.getElementById("pull-model-name").disabled = true;
    document.getElementById("models-hint").style.display = "none";
    document.getElementById("pull-progress").style.display = "block";
    var st = document.getElementById("pull-progress-text");
    var fe = document.getElementById("pull-progress-fill");
    st.innerHTML = '<span class=pull-spinner></span> Starting pull for ' + n + '...';
    fe.style.width = "0%";
    try {
        pullAbort = new AbortController();
        var r = await fetch("/api/ollama/pull", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: n}), signal: pullAbort.signal});
        if (!r.ok) { var e = await r.text(); throw new Error(e || "HTTP " + r.status); }
        var reader = r.body.getReader(), decoder = new TextDecoder(), buf = "";
        while (true) {
            var chunk = await reader.read();
            if (chunk.done) break;
            buf += decoder.decode(chunk.value, {stream: true});
            var ls = buf.split("\n"); buf = ls.pop() || "";
            for (var i = 0; i < ls.length; i++) {
                var ln = ls[i];
                if (ln.startsWith("data: ")) {
                    try {
                        var d = JSON.parse(ln.slice(6));
                        if (d.error) { fe.style.width = "0%"; st.innerHTML = d.error; st.className = "pull-progress-text pull-progress-error"; }
                        else if (d.status === "success") { fe.style.width = "100%"; st.innerHTML = "✅ Successfully pulled " + n; st.className = "pull-progress-text pull-progress-done"; setTimeout(refreshModels, 500); }
                        else if (d.total && d.completed !== undefined) { var pct = d.total > 0 ? Math.round(d.completed / d.total * 100) : 0; fe.style.width = pct + "%"; var m = d.status || "pulling..."; var dg = (d.digest || "").slice(0, 12); st.innerHTML = "<span class=pull-spinner></span> " + m + (dg ? " " + dg : ""); st.className = "pull-progress-text"; }
                        else { st.innerHTML = "<span class=pull-spinner></span> " + (d.status || "pulling..."); st.className = "pull-progress-text"; }
                    } catch (e) {}
                }
            }
        }
    } catch (e) {
        if (e.name !== "AbortError") { st.innerHTML = "❌ " + (e.message || "Pull failed"); st.className = "pull-progress-text pull-progress-error"; }
    } finally { resetPullUI(); }
}

function cancelPull() {
    if (pullAbort) { pullAbort.abort(); pullAbort = null; }
    document.getElementById("pull-progress-text").innerHTML = "❌ Pull cancelled";
    document.getElementById("pull-progress-text").className = "pull-progress-text pull-progress-error";
    resetPullUI();
}

function resetPullUI() {
    pullAbort = null;
    document.getElementById("pull-btn").style.display = "inline-block";
    document.getElementById("cancel-btn").style.display = "none";
    document.getElementById("pull-model-name").disabled = false;
}

function refreshModels() {
    fetch("/api/ollama/models")
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var list = document.getElementById("models-list");
            if (!data.models || !data.models.length) {
                list.innerHTML = '<div class=model-item><span class=model-name style=color:var(--text-muted)>No models installed</span></div>';
                return;
            }
            var h = "";
            for (var j = 0; j < data.models.length; j++) {
                var m2 = data.models[j];
                var gb = (m2.size / 1e9).toFixed(1);
                var escName = m2.name.replace(/"/g, "&quot;");
                h += '<div class=model-item><span class=model-name>' + m2.name + '</span><span class=model-size>' + gb + ' GB</span><button class=model-bench-btn data-model-name="' + escName + '" title="Benchmark ' + m2.name + '">⚡ Benchmark</button><button class=model-delete-btn data-name="' + escName + '" title="Delete ' + m2.name + '">🗑</button></div>';
            }
            list.innerHTML = h;
        })
        .catch(function(e) { console.error("refreshModels failed", e); });
}

// Configurable callback for delete confirmation — set this to wire in the API call.
// Signature: onModelDelete(name: string) => void
// The callback is invoked AFTER the confirmation dialog closes.
var onModelDelete = null;

function deleteModel(name) {
    showConfirmDialog({
        title: "Delete Model",
        description: 'Are you sure you want to delete "' + name + '"? This permanently removes the model from disk.',
        icon: "🗑",
        requireConfirmText: true,
        confirmText: name,
        labelConfirm: "Delete",
        labelCancel: "Cancel",
        onConfirm: function() {
            if (typeof onModelDelete === "function") {
                onModelDelete(name);
            }
        }
    });
}

// ── Delegated click handler for delete buttons ──
document.addEventListener("DOMContentLoaded", function() {
    var pullInput = document.getElementById("pull-model-name");
    if (pullInput) {
        pullInput.addEventListener("keydown", function(e) {
            if (e.key === "Enter" && !pullAbort) startPull();
        });
    }
    var modelsList = document.getElementById("models-list");
    if (modelsList) {
        modelsList.addEventListener("click", function(e) {
            var btn = e.target.closest(".model-delete-btn");
            if (!btn) return;
            e.stopPropagation();
            var name = btn.getAttribute("data-name");
            if (name) deleteModel(name);
        });
    }
});
