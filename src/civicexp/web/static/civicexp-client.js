/*
 * Civic Experimentation Platform — JavaScript client.
 *
 * Works in browsers and in Node.js 18+ (anything with fetch). No
 * dependencies.
 *
 * The one rule this client exists to enforce: an evaluation must never
 * break the service. If the platform is slow, unreachable, or answers with
 * an error, assign() resolves to the current version and record() resolves
 * quietly. A dropped measurement costs a little statistical power; a broken
 * page costs someone their application.
 *
 *   const cx = new CivicExp({ baseUrl, apiKey, experimentId, control: "control" });
 *   const { serve } = await cx.assign(caseNumber, { channel: "web", preferred_language: "es" });
 *   render(serve);
 *   ...
 *   cx.record(caseNumber, "submission_succeeded");
 *
 * Prefer calling from your own server, where the key stays private. A key
 * shipped to browsers is visible to anyone: give it the collect scope only,
 * limited to one evaluation.
 *
 * Licensed under the Apache License 2.0.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.CivicExp = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function CivicExp(options) {
    if (!options || !options.baseUrl || !options.apiKey || !options.experimentId) {
      throw new Error("CivicExp needs baseUrl, apiKey, and experimentId");
    }
    this.baseUrl = String(options.baseUrl).replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.experimentId = options.experimentId;
    this.control = options.control || "control";
    this.timeoutMs = options.timeoutMs || 800;
    this.onError = options.onError || function () {};
  }

  CivicExp.prototype._post = function (path, body) {
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, this.timeoutMs) : null;
    var url = this.baseUrl + "/api/v1/experiments/" + encodeURIComponent(this.experimentId) + path;
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + this.apiKey },
      body: JSON.stringify(body),
      signal: controller ? controller.signal : undefined,
      credentials: "omit",
      keepalive: true
    }).then(function (response) {
      if (timer) { clearTimeout(timer); }
      return response.json().then(function (data) {
        if (!response.ok) {
          var error = new Error((data && data.error && data.error.message) || response.statusText);
          error.code = data && data.error && data.error.code;
          throw error;
        }
        return data;
      });
    }, function (error) {
      if (timer) { clearTimeout(timer); }
      throw error;
    });
  };

  /**
   * Which version should this person see? Always resolves; on any failure
   * it resolves to { serve: control, inAnalysis: false, failedOpen: true }.
   */
  CivicExp.prototype.assign = function (unitId, attributes) {
    var self = this;
    return this._post("/assign", { unit_id: String(unitId), attributes: attributes || {} })
      .then(function (data) {
        return {
          serve: data.serve,
          variant: data.variant,
          inAnalysis: data.in_analysis,
          collecting: data.collecting,
          reason: data.reason,
          failedOpen: false
        };
      })
      .catch(function (error) {
        self.onError(error);
        return { serve: self.control, variant: null, inAnalysis: false, collecting: false,
                 reason: "failed open: " + error.message, failedOpen: true };
      });
  };

  /**
   * Record an outcome for a person the pilot enrolled. Resolves to true if
   * it was recorded, false otherwise; never rejects.
   */
  CivicExp.prototype.record = function (unitId, eventType, payload) {
    var self = this;
    var body = { unit_id: String(unitId), event_type: eventType };
    if (payload) { body.payload = payload; }
    return this._post("/events", body)
      .then(function () { return true; })
      .catch(function (error) { self.onError(error); return false; });
  };

  return CivicExp;
});
