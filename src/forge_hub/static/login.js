"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  let accessToken = null; // memory only; no localStorage, cookies or URL tokens
  let config = null;
  function message(text, error = false) {
    $("message").textContent = text;
    $("message").dataset.error = String(error);
  }
  async function api(path, data, token) {
    const headers = {};
    if (data !== undefined) headers["Content-Type"] = "application/json";
    if (token) headers.Authorization = "Bearer " + token;
    const response = await fetch(path, { method: data === undefined ? "GET" : "POST", headers,
      body: data === undefined ? undefined : JSON.stringify(data), credentials: "omit", redirect: "error" });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || "Request failed");
    return value;
  }
  async function submit(signup) {
    const email = $("email").value.trim(), password = $("password").value;
    if (!$("login-form").reportValidity()) return;
    if (signup && password.length < 12) { message("Use at least 12 characters for a new password.", true); return; }
    $("submit").disabled = true; $("signup").disabled = true; message(signup ? "Creating account…" : "Signing in…");
    try {
      const result = await api(signup ? "/v1/auth/signup" : "/v1/auth/login", { email, password });
      $("password").value = "";
      if (signup) { message(result.message); return; }
      accessToken = result.access_token;
      const profile = await api("/v1/me", undefined, accessToken);
      $("account-email").textContent = profile.email || "Signed in";
      $("login-form").hidden = true; $("profile").hidden = false;
      message("Signed in on this page. Use forge account login to sign in from your terminal.");
    } catch (error) { message(error.message, true); }
    finally { $("submit").disabled = !config?.accounts_configured; $("signup").disabled = !config?.signup_enabled; }
  }
  $("login-form").addEventListener("submit", (event) => { event.preventDefault(); submit(false); });
  $("signup").addEventListener("click", () => submit(true));
  $("logout").addEventListener("click", async () => {
    try { if (accessToken) await api("/v1/auth/logout", {}, accessToken); message("Signed out."); }
    catch (error) { message("Local session cleared; server sign-out could not be confirmed. " + error.message, true); }
    finally { accessToken = null; $("login-form").hidden = false; $("profile").hidden = true; }
  });
  api("/v1/status").then((value) => {
    config = value;
    $("availability").textContent = value.accounts_configured ? "Account access is separate from local coding." :
      "Account sign-in is not enabled yet. You can still install Forge and code locally with your own API key.";
    $("submit").disabled = !value.accounts_configured; $("signup").disabled = !value.signup_enabled;
  }).catch(() => { $("availability").textContent = "The account service is unavailable. Try again later."; });
})();
