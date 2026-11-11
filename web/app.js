/* homelab-dash page. Read-only: it only ever GETs status.json and history.json.
   All text from the server is inserted with textContent (never innerHTML). */
"use strict";

(function () {
  var REFRESH_MS = 30000;
  var FETCH_TIMEOUT_MS = 10000;
  var HISTORY_EVERY_MS = 120000;
  var DEFAULT_STALE_S = 180;

  var last = null;          // last good status.json
  var serverOffset = 0;     // server clock minus phone clock, in ms
  var lastOkAt = 0;         // phone time of the last successful fetch
  var inflight = false;
  var timer = null;
  var historyAt = 0;
  var lastError = null;

  // ------------------------------------------------------------ helpers
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function svgEl(tag, attrs) {
    var e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function bytes(n) {
    if (!isNum(n)) return "–";
    var u = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"], i = 0;
    while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(n >= 100 ? 0 : 1) : n) + " " + u[i];
  }
  function bits(n) {
    if (!isNum(n)) return "–";
    var u = ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"], i = 0;
    while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
    return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
  }
  function compact(n) {
    if (!isNum(n)) return "–";
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(1) + "K";
    return n.toLocaleString();
  }
  function pct(n) { return isNum(n) ? n.toFixed(n >= 10 ? 0 : 1) + "%" : "–"; }
  function dur(s) {
    if (!isNum(s)) return "–";
    s = Math.floor(s);
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + m + "m";
    if (m) return m + "m";
    return s + "s";
  }
  function ago(s) { return s < 60 ? Math.max(0, Math.round(s)) + " s ago" : dur(s) + " ago"; }
  function clock(t) {
    var d = new Date(t * 1000);
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }
  function serverNow() { return (Date.now() + serverOffset) / 1000; }

  var LABELS = { ok: ["✓", "OK"], warn: ["!", "Warning"], crit: ["✕", "Problem"],
                 error: ["✕", "Check failed"], unknown: ["–", "Unknown"] };
  function badge(status, text) {
    var b = el("span", "badge");
    var l = LABELS[status] || LABELS.unknown;
    b.dataset.status = status || "unknown";
    b.appendChild(el("span", "ico", l[0])).setAttribute("aria-hidden", "true");
    b.appendChild(el("span", "lbl", text || l[1]));
    return b;
  }
  function setBadge(node, status, text) {
    var l = LABELS[status] || LABELS.unknown;
    node.dataset.status = status || "unknown";
    node.querySelector(".ico").textContent = l[0];
    node.querySelector(".lbl").textContent = text || l[1];
  }
  function stat(parent, k, v, s) {
    var d = el("div", "stat");
    d.appendChild(el("div", "k", k));
    d.appendChild(el("div", "v", v));
    if (s) d.appendChild(el("div", "s", s));
    parent.appendChild(d);
    return d;
  }
  function meter(value, status, label) {
    var m = el("div", "meter");
    m.dataset.status = status || "ok";
    m.setAttribute("role", "meter");
    m.setAttribute("aria-valuemin", "0");
    m.setAttribute("aria-valuemax", "100");
    m.setAttribute("aria-valuenow", isNum(value) ? String(Math.round(value)) : "0");
    if (label) m.setAttribute("aria-label", label);
    var f = el("i");
    f.style.width = Math.max(0, Math.min(100, isNum(value) ? value : 0)) + "%";
    m.appendChild(f);
    return m;
  }
  function row(list, name, sub, val, status, link) {
    var li = el("li");
    if (status) li.appendChild(badgeDot(status)); else li.appendChild(el("span", "nodot"));
    var n = el("div", "name");
    if (link && /^https?:\/\//i.test(link)) {
      var a = el("a", null, name);
      a.href = link; a.rel = "noopener noreferrer"; a.target = "_blank";
      n.appendChild(a);
    } else {
      n.appendChild(document.createTextNode(name));
    }
    if (sub) n.appendChild(el("small", null, sub));
    li.appendChild(n);
    if (val !== undefined && val !== null && val !== "") li.appendChild(el("div", "val", val));
    list.appendChild(li);
    return li;
  }
  function badgeDot(status) {
    // compact status marker: icon + screen-reader label (never colour alone)
    var b = badge(status, "");
    b.querySelector(".lbl").textContent = "";
    b.setAttribute("title", (LABELS[status] || LABELS.unknown)[1]);
    b.setAttribute("aria-label", (LABELS[status] || LABELS.unknown)[1]);
    b.style.padding = "0"; b.style.border = "0"; b.style.background = "transparent";
    return b;
  }
  function section(parent, text) { parent.appendChild(el("div", "section-label", text)); }

  // ------------------------------------------------------------ cards
  var R = {};

  R.system = function (body, d) {
    if (d.os || d.kernel) body.appendChild(el("p", "sub", [d.os, d.kernel, d.cpu_model].filter(Boolean).join(" · ")));
    var s = el("div", "stats");
    stat(s, "Uptime", dur(d.uptime_s));
    stat(s, "CPU", pct(d.cpu_pct), d.cores ? d.cores + " cores" : null);
    stat(s, "Load", d.load ? d.load.map(function (x) { return x.toFixed(2); }).join(" ") : "–", "1 · 5 · 15 min");
    stat(s, "RAM", d.mem ? pct(d.mem.pct) : "–", d.mem ? bytes(d.mem.used) + " of " + bytes(d.mem.total) : null);
    body.appendChild(s);
    if (isNum(d.cpu_pct)) { body.appendChild(el("div", "k sub", "CPU")); body.appendChild(meter(d.cpu_pct, d.cpu_status, "CPU")); }
    if (d.mem) {
      body.appendChild(el("div", "k sub", "Memory"));
      body.appendChild(meter(d.mem.pct, d.mem.status, "Memory"));
      if (d.mem.swap_total) body.appendChild(el("div", "sub", "Swap " + bytes(d.mem.swap_used) + " of " + bytes(d.mem.swap_total)));
    }
    if (d.temps && d.temps.length) {
      section(body, "Temperatures");
      var ul = el("ul", "rows");
      d.temps.slice(0, 12).forEach(function (t) { row(ul, t.label, t.chip, t.c.toFixed(0) + " °C", t.status); });
      body.appendChild(ul);
    }
    var ul2 = el("ul", "rows");
    if (d.failed_units) {
      row(ul2, "Failed systemd units", d.failed_units.join(", ") || null,
          String(d.failed_units.length), d.failed_units.length ? "warn" : "ok");
    }
    if (isNum(d.journal_errors_1h)) {
      row(ul2, "Log errors (last hour)", null, d.journal_errors_1h >= 1000 ? "1000+" : String(d.journal_errors_1h),
          d.journal_errors_1h > 50 ? "warn" : "ok");
    }
    if (ul2.children.length) { section(body, "Health"); body.appendChild(ul2); }
  };

  R.disks = function (body, d) {
    if (d.mounts && d.mounts.length) {
      section(body, "Space");
      d.mounts.forEach(function (m) {
        var line = el("div", "stat");
        var head = el("div", "card-head");
        var name = el("div", "name");
        name.appendChild(document.createTextNode(m.mount));
        name.appendChild(el("span", "tag", m.fs || "?"));
        head.appendChild(name);
        head.appendChild(el("div", "val", isNum(m.avail) ? bytes(m.avail) + " free · " + pct(m.pct) : "unavailable"));
        head.style.marginBottom = "0";
        line.appendChild(head);
        line.appendChild(meter(m.pct, m.status, m.mount + " used"));
        line.style.marginBottom = ".5rem";
        body.appendChild(line);
      });
    }
    if (d.smart && d.smart.length) {
      section(body, "Drive health (SMART)");
      var ul = el("ul", "rows");
      d.smart.forEach(function (s) {
        var bits2 = [];
        if (s.passed === true) bits2.push("passed");
        if (s.passed === false) bits2.push("FAILED");
        if (isNum(s.temp)) bits2.push(s.temp + " °C");
        if (isNum(s.hours)) bits2.push(Math.round(s.hours / 24) + " days on");
        if (isNum(s.pct_used)) bits2.push(s.pct_used + "% worn");
        if (s.standby) bits2.push("asleep");
        var sub = [s.model, s.reasons && s.reasons.length ? s.reasons.join("; ") : null, s.note, s.error]
          .filter(Boolean).join(" · ");
        row(ul, s.device, sub, bits2.join(" · "), s.status);
      });
      body.appendChild(ul);
    }
    if (d.raid && d.raid.length) {
      section(body, "Arrays & pools");
      var ul3 = el("ul", "rows");
      d.raid.forEach(function (r) { row(ul3, r.name, r.type + (r.level ? " " + r.level : ""), r.state + (r.detail ? " · " + r.detail : ""), r.status); });
      body.appendChild(ul3);
    }
  };

  R.services = function (body, d) {
    body.appendChild(el("p", "sub", d.up + " of " + d.total + " up"));
    var groups = {};
    var order = [];
    (d.items || []).forEach(function (i) {
      var g = i.group || "";
      if (!groups[g]) { groups[g] = []; order.push(g); }
      groups[g].push(i);
    });
    order.forEach(function (g) {
      if (g) section(body, g);
      var ul = el("ul", "rows");
      groups[g].forEach(function (i) { row(ul, i.label, i.type, i.detail, i.status, i.link); });
      body.appendChild(ul);
    });
  };

  R.docker = function (body, d) {
    body.appendChild(el("p", "sub", d.running + " of " + d.total + " containers running"));
    (d.stacks || []).forEach(function (g) {
      var det = el("details", "stack");
      if (g.status === "crit" || g.status === "warn") det.open = true;
      var sum = el("summary");
      sum.appendChild(badgeDot(g.status));
      sum.appendChild(el("span", "name", g.name));
      sum.appendChild(el("span", "val", g.running + "/" + g.total));
      det.appendChild(sum);
      var ul = el("ul", "rows");
      g.containers.forEach(function (c) {
        var sub = [c.image];
        if (c.network_via) sub.push("network via " + c.network_via);
        if (c.provides_network_to) sub.push("network for " + c.provides_network_to.join(", "));
        (c.macvlan || []).forEach(function (m) { sub.push(m.driver + " " + (m.ip || "")); });
        var li = row(ul, c.name, sub.join(" · "), c.detail, c.status);
        if (c.role !== "main") li.querySelector(".name").insertBefore(el("span", "tag", c.role), li.querySelector(".name small"));
      });
      det.appendChild(ul);
      body.appendChild(det);
    });
    if (d.vlan_networks && d.vlan_networks.length) {
      var names = d.vlan_networks.map(function (n) { return n.name + " (" + n.driver + (n.parent ? " on " + n.parent : "") + ")"; });
      body.appendChild(el("p", "note", "macvlan/ipvlan: " + names.join(", ") +
        ". The server can't reach these containers' IPs directly (normal for macvlan), so their state comes from Docker."));
    }
  };

  R.network = function (body, d) {
    var s = el("div", "stats");
    stat(s, "Down", bits(d.rx_bps));
    stat(s, "Up", bits(d.tx_bps));
    if (d.gateway) stat(s, "Gateway", d.gateway.gateway, d.gateway.iface);
    if (d.dns) stat(s, "DNS", d.dns.ok ? d.dns.ms + " ms" : "failing", d.dns.name);
    body.appendChild(s);
    if (d.dns && !d.dns.ok) body.appendChild(el("p", "note", "DNS lookup failed: " + d.dns.error));
    var ul = el("ul", "rows");
    (d.interfaces || []).forEach(function (i) {
      var sub = (i.addrs || []).join(", ");
      var val = i.state + (i.speed_mbps ? " · " + (i.speed_mbps >= 1000 ? i.speed_mbps / 1000 + " Gb/s" : i.speed_mbps + " Mb/s") : "");
      if (isNum(i.rx_bps)) val += " · ↓" + bits(i.rx_bps) + " ↑" + bits(i.tx_bps);
      row(ul, i.name, sub, val, i.status);
    });
    if (ul.children.length) { section(body, "Interfaces"); body.appendChild(ul); }
    if (d.tailscale) {
      var t = d.tailscale;
      section(body, "Tailscale");
      var ul2 = el("ul", "rows");
      row(ul2, t.dns_name || t.hostname || "tailscale", (t.ips || []).join(", "),
          t.state + " · " + t.peers_online + "/" + t.peers_total + " peers online", t.status);
      (t.health || []).forEach(function (h) { row(ul2, "Health", null, h, "warn"); });
      body.appendChild(ul2);
    }
  };

  R.pihole = function (body, d) {
    var s = el("div", "stats");
    stat(s, "Queries today", compact(d.queries));
    stat(s, "Blocked", compact(d.blocked), pct(d.pct_blocked));
    stat(s, "Clients", compact(d.clients));
    stat(s, "Blocklist", compact(d.domains_blocked), "domains");
    body.appendChild(s);
    body.appendChild(meter(d.pct_blocked, "ok", "Share of queries blocked"));
    var ul = el("ul", "rows");
    row(ul, "Blocking", "Pi-hole v" + d.version, d.blocking || "unknown", d.blocking === "enabled" ? "ok" : (d.blocking ? "warn" : "unknown"));
    if (isNum(d.gravity_updated)) row(ul, "Blocklists updated", null, ago(serverNow() - d.gravity_updated), "ok");
    if (d.local_dns && d.local_dns.length) row(ul, "Local DNS records", null, String(d.local_dns.length), "ok");
    body.appendChild(ul);
  };

  R.media = function (body, d) {
    var s = el("div", "stats");
    stat(s, "Playing", String(d.active));
    stat(s, "Paused", String(d.paused));
    stat(s, "Transcoding", String(d.transcoding));
    body.appendChild(s);
    if (d.streams && d.streams.length) {
      var ul = el("ul", "rows");
      d.streams.forEach(function (x) {
        row(ul, x.title, x.user, (x.paused ? "paused" : "playing") + (x.transcode ? " · transcode" : ""), "ok");
      });
      body.appendChild(ul);
    }
  };

  R.games = function (body, d) {
    body.appendChild(el("p", "sub", d.players + " player" + (d.players === 1 ? "" : "s") + " online"));
    var ul = el("ul", "rows");
    (d.servers || []).forEach(function (g) {
      var val = g.online ? (isNum(g.players) ? g.players + (isNum(g.max_players) ? "/" + g.max_players : "") + " players" : "online")
                         : (g.detail || "offline");
      var sub = [g.server_name, g.map, g.version].filter(Boolean).join(" · ");
      row(ul, g.label, sub, val, g.status);
    });
    body.appendChild(ul);
  };

  R.addresses = function (body, d) {
    (d.networks || []).forEach(function (n, i) {
      if (i === 0) section(body, "Free IPs on macvlan / ipvlan");
      var ul = el("ul", "rows");
      row(ul, n.name, n.driver + (n.parent ? " on " + n.parent : "") + " · pool " + n.pool +
          (n.pool !== n.subnet ? " in " + n.subnet : "") + (n.gateway ? " · gateway " + n.gateway : ""),
          n.free_count + " free of " + n.pool_size, n.status);
      body.appendChild(ul);
      if (n.free_ranges && n.free_ranges.length) {
        var chips = el("div", "chips");
        n.free_ranges.forEach(function (r) { chips.appendChild(el("code", "chip", r)); });
        if (n.more_ranges) chips.appendChild(el("span", "sub", "+" + n.more_ranges + " more ranges"));
        body.appendChild(chips);
      }
      (n.notes || []).forEach(function (t) { body.appendChild(el("p", "note", t)); });
      if (n.used && n.used.length) {
        var det = el("details", "stack");
        var sum = el("summary");
        sum.appendChild(el("span", "name", "In use (" + n.used.length + ")"));
        det.appendChild(sum);
        var ul2 = el("ul", "rows");
        n.used.forEach(function (u) { row(ul2, u.ip, u.note || null, u.name, u.note ? "unknown" : "ok"); });
        det.appendChild(ul2);
        body.appendChild(det);
      }
    });
    if (d.domains_enabled) {
      section(body, "Domains (*." + (d.suffix || "home.arpa") + ")");
      if (!d.domains || !d.domains.length) {
        body.appendChild(el("p", "sub", "No names found in Pi-hole local DNS, /etc/hosts or [domains] extra."));
      } else {
        var ul3 = el("ul", "rows");
        d.domains.forEach(function (x) {
          // listed, not checked: no status icon
          var li = row(ul3, x.name, (x.targets || []).join(", ") || null, x.sources.join(" · "), null, x.url);
          li.classList.add("domain");
        });
        body.appendChild(ul3);
      }
    }
  };

  R.security = function (body, d) {
    var u = d.updates || {}, rb = d.reboot || {}, fw = d.firewall, lg = d.logins || {};
    var s = el("div", "stats");
    stat(s, "Security updates", isNum(u.security) ? String(u.security) : "–", isNum(u.pending) ? u.pending + " updates in all" : null);
    stat(s, "Reboot", rb.required ? "needed" : "no");
    stat(s, "Failed SSH logins", isNum(lg.failed_24h) ? String(lg.failed_24h) : "–", "last 24 h");
    stat(s, "Firewall", fw ? (fw.active ? (fw.name + " on") : "OFF") : "–", fw && isNum(fw.rules) ? fw.rules + " rules" : null);
    body.appendChild(s);

    function worstOf(rows) {
      var w = "ok";
      rows.forEach(function (r) {
        if (r.status === "crit" || r.status === "error") w = "crit";
        else if (r.status === "warn" && w !== "crit") w = "warn";
      });
      return w;
    }
    function fold(title, rows, render) {   // collapsible list; opens itself when something is wrong
      if (!rows || !rows.length) return;
      var det = el("details", "stack");
      var st = worstOf(rows);
      if (st !== "ok") det.open = true;
      var sum = el("summary");
      sum.appendChild(badgeDot(st));
      sum.appendChild(el("span", "name", title));
      sum.appendChild(el("span", "val", String(rows.length)));
      det.appendChild(sum);
      var ul = el("ul", "rows");
      rows.forEach(function (r) { render(ul, r); });
      det.appendChild(ul);
      body.appendChild(det);
    }

    section(body, "Updates & reboot");
    var ul = el("ul", "rows");
    if (d.updates) {
      row(ul, "Package updates", u.manager + (u.auto_updates === true ? " · automatic security updates on" :
          u.auto_updates === false ? " · automatic security updates OFF" : ""),
          (isNum(u.pending) ? u.pending + " pending · " : "") + (isNum(u.security) ? u.security + " security" : ""),
          u.security ? "warn" : "ok");
      if (isNum(u.lists_age_days)) row(ul, "Package lists", null, u.lists_age_days + " days old", u.lists_age_days > 7 ? "warn" : "ok");
    }
    if (d.reboot) {
      row(ul, "Reboot", rb.kernel_newest && rb.kernel_newest !== rb.kernel_running ?
          "running " + rb.kernel_running + ", installed " + rb.kernel_newest : (rb.packages || []).join(", ") || null,
          rb.required ? "needed" : "not needed", rb.required ? "warn" : "ok");
    }
    body.appendChild(ul);

    section(body, "Firewall & open ports");
    var ul2 = el("ul", "rows");
    if (fw) row(ul2, "Firewall", fw.name || "none found", fw.active ? "on" : "OFF", fw.active ? "ok" : "crit");
    body.appendChild(ul2);
    if (d.docker_bypasses_firewall) {
      body.appendChild(el("p", "note", "Ports published by Docker skip ufw: they are reachable from everywhere " +
        "this server is, whatever ufw says. Publish them on 127.0.0.1 or a LAN IP if they shouldn't be."));
    }
    fold("Listening on the network", d.ports, function (list, p) {
      var sub = (p.hint ? p.hint + " · " : "") + (p.families || []).join(" + ");
      var val = p.docker ? "Docker: " + p.docker + " (skips ufw)" : (p.expected ? "expected" : "not expected");
      row(list, "port " + p.port, sub, val, p.status);
    });

    section(body, "Logins & SSH");
    var ul3 = el("ul", "rows");
    if (d.ssh_config) {
      var c = d.ssh_config;
      row(ul3, "sshd settings", "root login: " + c.permit_root_login + " · passwords: " + c.password_auth,
          (c.findings || []).join("; ") || "keys only, no root password", c.status);
    }
    if (d.logins) {
      row(ul3, "Failed logins (24 h)", (lg.top_sources || []).map(function (t) { return t.ip + " ×" + t.count; }).join(", ") || null,
          String(lg.failed_24h), lg.failed_24h > 20 ? "warn" : "ok");
      row(ul3, "Successful logins (24 h)", lg.last_accepted ? "last: " + lg.last_accepted : null, String(lg.accepted_24h), "ok");
    }
    if (d.fail2ban && d.fail2ban.jails) {
      Object.keys(d.fail2ban.jails).forEach(function (j) {
        row(ul3, "fail2ban: " + j, null, (d.fail2ban.jails[j] === null ? "?" : d.fail2ban.jails[j]) + " banned now", "ok");
      });
    }
    body.appendChild(ul3);

    fold("Exposure (systemd-analyze security)", d.exposure, function (list, e) {
      var sub = e.dashboard ? "this dashboard: must stay at or below its install score" :
        "informational: most stock services score 9 or more";
      row(list, e.unit, sub, (isNum(e.score) ? e.score.toFixed(1) + " " : "") + e.label, e.dashboard ? e.status : null);
    });
    fold("Permissions", d.permissions, function (list, r) {
      row(list, r.what, r.path, r.detail, r.status);
    });
    fold("Containers with risky settings", d.containers, function (list, c) {
      row(list, c.name, null, c.risks.join("; "), c.status);
    });
  };

  R.error = function () {};

  function renderCard(c) {
    var card = el("section", "card");
    card.dataset.status = c.status;
    card.id = "card-" + c.id;
    var head = el("div", "card-head");
    head.appendChild(el("h3", null, c.title));
    head.appendChild(badge(c.status));
    card.appendChild(head);
    var body = el("div", "body");
    try {
      (R[c.type] || R.error)(body, c.data || {});
    } catch (e) {
      body.appendChild(el("p", "note", "Could not display this card: " + e.message));
    }
    card.appendChild(body);
    if (c.errors && c.errors.length) {
      var ul = el("ul", "errors");
      c.errors.forEach(function (e) { ul.appendChild(el("li", null, e.check + ": " + e.error)); });
      card.appendChild(ul);
    }
    return card;
  }

  function render(s) {
    $("host").textContent = s.hostname || "Home lab";
    document.title = (s.status && s.status !== "ok" ? "⚠ " : "") + (s.hostname || "Home lab");
    var overall = s.status || "unknown";
    setBadge($("overall"), overall, { ok: "All good", warn: "Warning", crit: "Problem", error: "Check failed" }[overall]);
    $("version").textContent = "homelab-dash " + (s.version || "");
    var frag = document.createDocumentFragment();
    (s.cards || []).forEach(function (c) { frag.appendChild(renderCard(c)); });
    var main = $("cards");
    // keep open/closed state of Docker stacks across refreshes
    var open = {};
    main.querySelectorAll("details.stack").forEach(function (d) { open[d.querySelector(".name").textContent] = d.open; });
    main.replaceChildren(frag);
    main.querySelectorAll("details.stack").forEach(function (d) {
      var k = d.querySelector(".name").textContent;
      if (k in open) d.open = open[k];
    });
  }

  // ------------------------------------------------------------ freshness
  function tick() {
    if (!last) return;
    var age = serverNow() - last.generated_at;
    var staleAfter = last.stale_after || DEFAULT_STALE_S;
    $("updated").textContent = "updated " + ago(age) + " · " + clock(last.generated_at);
    var stale = age > staleAfter;
    $("banner-stale").hidden = !stale;
    if (stale) $("stale-text").textContent = "The server last reported " + ago(age) +
      ". The collector may have stopped: check it with  systemctl status homelab-dash-collector.timer";
    if (stale) setBadge($("overall"), "warn", "Stale");
  }

  function showError(msg) {
    lastError = msg;
    $("banner-error").hidden = false;
    $("error-text").textContent = msg + (lastOkAt ? " Showing data from " + ago((Date.now() - lastOkAt) / 1000) + "." : "");
    $("cards").classList.add("dim");
    if (!last) setBadge($("overall"), "error", "Offline");
  }
  function clearError() {
    lastError = null;
    $("banner-error").hidden = true;
    $("cards").classList.remove("dim");
  }

  function getJSON(url) {
    var ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var to = setTimeout(function () { if (ctl) ctl.abort(); }, FETCH_TIMEOUT_MS);
    var sentAt = Date.now();
    return fetch(url, { cache: "no-store", credentials: "omit", signal: ctl ? ctl.signal : undefined })
      .then(function (r) {
        if (!r.ok) {
          return r.text().then(function (t) { throw new Error("HTTP " + r.status + (t ? ": " + t.trim().slice(0, 120) : "")); });
        }
        var date = Date.parse(r.headers.get("Date") || "");
        if (isFinite(date)) {
          // Date has 1 s resolution; use the midpoint of the request
          serverOffset = date + 500 - (sentAt + Date.now()) / 2;
        }
        return r.json();
      })
      .catch(function (e) {
        if (e && e.name === "AbortError") throw new Error("The server didn't answer within " + FETCH_TIMEOUT_MS / 1000 + " s.");
        if (e instanceof TypeError) throw new Error("The server is unreachable (are you on the home network or Tailscale?).");
        throw e;
      })
      .finally(function () { clearTimeout(to); });
  }

  function load() {
    if (inflight) return;
    inflight = true;
    getJSON("status.json")
      .then(function (s) {
        if (!s || !Array.isArray(s.cards)) throw new Error("status.json has an unexpected format.");
        var changed = !last || last.generated_at !== s.generated_at;
        last = s;
        lastOkAt = Date.now();
        clearError();
        if (changed) render(s);
        tick();
        if (Date.now() - historyAt > HISTORY_EVERY_MS) loadHistory();
      })
      .catch(function (e) { showError(e.message || String(e)); })
      .finally(function () { inflight = false; schedule(); });
  }

  function schedule() {
    clearTimeout(timer);
    if (document.visibilityState === "hidden") return; // resume on visibilitychange
    timer = setTimeout(load, REFRESH_MS);
  }

  // ------------------------------------------------------------ history
  var SERIES = [
    { key: "cpu", title: "CPU", unit: "%", fmt: pct, max: 100 },
    { key: "mem", title: "Memory", unit: "%", fmt: pct, max: 100 },
    { key: "temp", title: "Hottest sensor", fmt: function (v) { return isNum(v) ? v.toFixed(0) + " °C" : "–"; } },
    { key: "load", title: "Load (1 min)", fmt: function (v) { return isNum(v) ? v.toFixed(2) : "–"; } },
    { key: "disk", title: "Fullest disk", fmt: pct, max: 100 },
    { keys: ["rx", "tx"], names: ["Down", "Up"], title: "Network", fmt: bits }
  ];

  function loadHistory() {
    historyAt = Date.now();
    getJSON("history.json").then(drawHistory).catch(function () { /* history is optional */ });
  }

  function drawHistory(h) {
    if (!h || !Array.isArray(h.samples) || h.samples.length < 2) return;
    var idx = {};
    h.fields.forEach(function (f, i) { idx[f] = i; });
    var raw = h.samples;
    var tEnd = raw[raw.length - 1][0];
    var tStart = tEnd - (h.hours || 24) * 3600;
    var gap = Math.max(180, 3 * ((last && last.interval) || 60));
    // Average into ~240 time buckets: one per ~1.5 px, so lines stay readable.
    var BUCKETS = 240, width = (tEnd - tStart) / BUCKETS;
    var samples = bucketize(raw, h.fields.length, tStart, width, BUCKETS);
    gap = Math.max(gap, width * 1.5);
    var box = $("charts");
    box.replaceChildren();
    SERIES.forEach(function (spec) {
      var keys = spec.keys || [spec.key];
      if (!keys.some(function (k) { return samples.some(function (s) { return isNum(s[idx[k]]); }); })) return;
      box.appendChild(chart(spec, keys, samples, idx, tStart, tEnd, gap));
    });
    drawTable(samples, idx);
    $("history").hidden = false;
  }

  function bucketize(raw, nf, t0, width, n) {
    var sums = [], counts = [], out = [];
    raw.forEach(function (s) {
      var b = Math.min(n - 1, Math.floor((s[0] - t0) / width));
      if (b < 0) return;
      if (!sums[b]) { sums[b] = new Array(nf).fill(0); counts[b] = new Array(nf).fill(0); }
      for (var i = 1; i < nf; i++) if (isNum(s[i])) { sums[b][i] += s[i]; counts[b][i]++; }
      counts[b][0]++;
      sums[b][0] = Math.max(sums[b][0], s[0]);
    });
    for (var b = 0; b < n; b++) {
      if (!sums[b]) continue;
      var row = [sums[b][0]];
      for (var i = 1; i < nf; i++) row.push(counts[b][i] ? sums[b][i] / counts[b][i] : null);
      out.push(row);
    }
    return out;
  }

  function chart(spec, keys, samples, idx, t0, t1, gap) {
    var W = 320, H = 110, PL = 34, PR = 6, PT = 8, PB = 18;
    var wrap = el("div", "chart");
    var head = el("div", "ch");
    head.appendChild(el("h3", null, spec.title));
    var latest = samples[samples.length - 1];
    head.appendChild(el("span", "now", keys.map(function (k) { return spec.fmt(latest[idx[k]]); }).join(" / ")));
    wrap.appendChild(head);
    if (keys.length > 1) {
      var lg = el("div", "legend");
      keys.forEach(function (k, i) {
        var s = el("span"); var sw = el("i", "sw" + (i + 1)); s.appendChild(sw);
        s.appendChild(document.createTextNode(spec.names[i])); lg.appendChild(s);
      });
      wrap.appendChild(lg);
    }
    var vmax = spec.max || 0, vmin = 0;
    samples.forEach(function (s) { keys.forEach(function (k) { var v = s[idx[k]]; if (isNum(v) && v > vmax) vmax = v; }); });
    if (!spec.max) vmax = vmax * 1.1 || 1;
    if (spec.key === "temp") {
      vmin = Infinity;
      samples.forEach(function (s) { var v = s[idx.temp]; if (isNum(v) && v < vmin) vmin = v; });
      vmin = isFinite(vmin) ? Math.max(0, Math.floor(vmin / 10) * 10 - 10) : 0;
    }
    function x(t) { return PL + (t - t0) / (t1 - t0) * (W - PL - PR); }
    function y(v) { return PT + (1 - (v - vmin) / ((vmax - vmin) || 1)) * (H - PT - PB); }
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none", role: "img",
      "aria-label": spec.title + " over the last 24 hours" });
    [vmin, (vmin + vmax) / 2, vmax].forEach(function (v) {
      svg.appendChild(svgEl("line", { x1: PL, x2: W - PR, y1: y(v), y2: y(v), "class": "gridline" }));
      var tx = svgEl("text", { x: PL - 4, y: y(v) + 3, "text-anchor": "end", "class": "axis" });
      tx.textContent = keys.length > 1 ? bits(v).replace("bit/s", "").trim() : (spec.max ? Math.round(v) : (v >= 10 ? Math.round(v) : v.toFixed(1)));
      svg.appendChild(tx);
    });
    [t0, (t0 + t1) / 2, t1].forEach(function (t, i) {
      var tx = svgEl("text", { x: x(t), y: H - 4, "text-anchor": ["start", "middle", "end"][i], "class": "axis" });
      tx.textContent = i === 2 ? "now" : clock(t);
      svg.appendChild(tx);
    });
    keys.forEach(function (k, si) {
      var d = "", prevT = null;
      samples.forEach(function (s) {
        var v = s[idx[k]];
        if (!isNum(v)) { prevT = null; return; }
        var cmd = (prevT === null || s[0] - prevT > gap) ? "M" : "L"; // break the line across outages
        d += cmd + x(s[0]).toFixed(1) + " " + y(v).toFixed(1);
        prevT = s[0];
      });
      svg.appendChild(svgEl("path", { d: d, "class": "line s" + (si + 1), "vector-effect": "non-scaling-stroke" }));
    });
    var cross = svgEl("line", { y1: PT, y2: H - PB, "class": "cross", visibility: "hidden" });
    svg.appendChild(cross);
    var dots = keys.map(function (k, si) {
      var c = svgEl("circle", { r: 4, "class": "dot s" + (si + 1), visibility: "hidden" });
      svg.appendChild(c); return c;
    });
    var tip = el("div", "tip"); tip.hidden = true;
    // hover / touch crosshair with the nearest sample
    function move(ev) {
      var r = svg.getBoundingClientRect();
      var px = (ev.clientX - r.left) / r.width * W;
      var t = t0 + (px - PL) / (W - PL - PR) * (t1 - t0);
      var best = null, bd = Infinity;
      for (var i = 0; i < samples.length; i++) {
        var dd = Math.abs(samples[i][0] - t);
        if (dd < bd) { bd = dd; best = samples[i]; }
      }
      if (!best || bd > gap) { leave(); return; }
      cross.setAttribute("x1", x(best[0])); cross.setAttribute("x2", x(best[0]));
      cross.setAttribute("visibility", "visible");
      keys.forEach(function (k, si) {
        var v = best[idx[k]];
        if (isNum(v)) { dots[si].setAttribute("cx", x(best[0])); dots[si].setAttribute("cy", y(v)); dots[si].setAttribute("visibility", "visible"); }
        else dots[si].setAttribute("visibility", "hidden");
      });
      tip.textContent = clock(best[0]) + "  " + keys.map(function (k, si) {
        return (spec.names ? spec.names[si] + " " : "") + spec.fmt(best[idx[k]]);
      }).join("  ");
      tip.hidden = false;
    }
    function leave() {
      cross.setAttribute("visibility", "hidden");
      dots.forEach(function (d) { d.setAttribute("visibility", "hidden"); });
      tip.hidden = true;
    }
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerdown", move);
    svg.addEventListener("pointerleave", leave);
    wrap.appendChild(svg);
    wrap.appendChild(tip);
    return wrap;
  }

  function drawTable(samples, idx) {
    var hours = {};
    samples.forEach(function (s) {
      var h = Math.floor(s[0] / 3600) * 3600;
      (hours[h] = hours[h] || []).push(s);
    });
    var table = el("table");
    var thead = el("tr");
    ["Hour", "CPU", "RAM", "Temp", "Load", "Disk", "Down", "Up"].forEach(function (t) { thead.appendChild(el("th", null, t)); });
    table.appendChild(thead);
    var keys = ["cpu", "mem", "temp", "load", "disk", "rx", "tx"];
    var fmts = [pct, pct, function (v) { return isNum(v) ? v.toFixed(0) + " °C" : "–"; },
                function (v) { return isNum(v) ? v.toFixed(2) : "–"; }, pct, bits, bits];
    Object.keys(hours).sort().reverse().forEach(function (h) {
      var tr = el("tr");
      tr.appendChild(el("td", null, clock(+h)));
      keys.forEach(function (k, i) {
        var vals = hours[h].map(function (s) { return s[idx[k]]; }).filter(isNum);
        tr.appendChild(el("td", null, vals.length ? fmts[i](vals.reduce(function (a, b) { return a + b; }, 0) / vals.length) : "–"));
      });
      table.appendChild(tr);
    });
    var sc = el("div", "scroll"); sc.appendChild(table);
    $("history-table").replaceChildren(sc);
  }

  // ------------------------------------------------------------ lifecycle
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") { tick(); load(); }
    else clearTimeout(timer);
  });
  window.addEventListener("pageshow", function (e) { if (e.persisted) load(); });
  window.addEventListener("online", load);
  window.addEventListener("focus", function () { if (Date.now() - lastOkAt > 5000) load(); });
  setInterval(function () { if (document.visibilityState !== "hidden") tick(); }, 5000);
  load();
})();
