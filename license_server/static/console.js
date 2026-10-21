// Small progressive enhancements only; every page works without them.
document.addEventListener("click", function (event) {
  var copy = event.target.closest("[data-copy],[data-copy-from]");
  if (!copy) return;
  var text = copy.getAttribute("data-copy");
  if (text === null) {
    var source = document.getElementById(copy.getAttribute("data-copy-from"));
    if (!source) return;
    source.select();
    text = source.value;
  }
  var done = function () {
    var old = copy.textContent;
    copy.textContent = "کپی شد";
    setTimeout(function () { copy.textContent = old; }, 1500);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, function () {});
  } else {
    try { document.execCommand("copy"); done(); } catch (e) { /* select() already highlights it */ }
  }
});

// A confirm button submits once: a double click must not send twice. The
// server refuses a repeat anyway (compare-and-set, one-time nonce).
document.addEventListener("submit", function (event) {
  var button = event.target.querySelector("[data-once]");
  if (!button) return;
  if (button.dataset.sent) { event.preventDefault(); return; }
  button.dataset.sent = "1";
  setTimeout(function () { button.disabled = true; button.textContent = "در حال ارسال…"; }, 0);
});
