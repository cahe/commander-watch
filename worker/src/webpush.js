// Web Push with nothing but Web Crypto: encrypts a message for one browser's subscription (RFC 8291, aes128gcm)
// and signs the request with our VAPID key (RFC 8292). The browser's push service (Google's, Mozilla's, Apple's
// or Microsoft's, whichever the browser uses) delivers it to the device, which shows it with sw.js.

const enc = new TextEncoder();

export const b64u = {
  decode(s) {
    const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
    return Uint8Array.from(bin, c => c.charCodeAt(0));
  },
  encode(bytes) {
    let bin = "";
    for (const b of bytes) bin += String.fromCharCode(b);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
};

const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
};

async function hkdf(salt, ikm, info, length) {
  const key = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, key, length * 8));
}

// The message, readable only by the browser that holds the subscription's private key.
export async function encrypt(p256dh, authSecret, plaintext) {
  const uaPublic = b64u.decode(p256dh), auth = b64u.decode(authSecret);
  const local = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const asPublic = new Uint8Array(await crypto.subtle.exportKey("raw", local.publicKey));
  const uaKey = await crypto.subtle.importKey("raw", uaPublic, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: uaKey }, local.privateKey, 256));
  const ikm = await hkdf(auth, shared, concat(enc.encode("WebPush: info\0"), uaPublic, asPublic), 32);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const cek = await hkdf(salt, ikm, enc.encode("Content-Encoding: aes128gcm\0"), 16);
  const nonce = await hkdf(salt, ikm, enc.encode("Content-Encoding: nonce\0"), 12);
  const key = await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["encrypt"]);
  // One record: the message, then the delimiter 2 that marks the last record.
  const sealed = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, key, concat(plaintext, new Uint8Array([2]))));
  const header = new Uint8Array(21 + asPublic.length);
  header.set(salt);
  new DataView(header.buffer).setUint32(16, 4096);  // record size
  header[20] = asPublic.length;
  header.set(asPublic, 21);
  return concat(header, sealed);
}

// Proves to the push service that the message comes from the key the browser subscribed with.
export async function vapidJwt(audience, subject, privateJwk) {
  const key = await crypto.subtle.importKey("jwk", privateJwk, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const part = (o) => b64u.encode(enc.encode(JSON.stringify(o)));
  const unsigned = `${part({ typ: "JWT", alg: "ES256" })}.${part({ aud: audience, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: subject })}`;
  const sig = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, enc.encode(unsigned)));
  return `${unsigned}.${b64u.encode(sig)}`;
}

// Sends one message. The push service answers 201 when it has it; 404 or 410 means the subscription is gone.
export async function sendPush(subscription, message, { privateJwk, publicKey, subject, ttl = 24 * 3600 }) {
  const body = await encrypt(subscription.keys.p256dh, subscription.keys.auth, enc.encode(JSON.stringify(message)));
  const jwt = await vapidJwt(new URL(subscription.endpoint).origin, subject, privateJwk);
  return fetch(subscription.endpoint, {
    method: "POST",
    headers: {
      Authorization: `vapid t=${jwt}, k=${publicKey}`,
      "Content-Encoding": "aes128gcm",
      "Content-Type": "application/octet-stream",
      TTL: String(ttl),  // a phone that's off for longer than a day misses the alert rather than getting a stale one
      Urgency: "normal",
    },
    body,
  });
}
