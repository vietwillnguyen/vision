// The QR the pendant scans to join Wi-Fi and pair. This is a contract with
// firmware's parse_onboarding_qr_payload (firmware/visio_recorder/
// wifi_onboard.py); see "QR payload v2" in
// docs/superpowers/specs/2026-11-19-visio-device-pairing-design.md.
// The pendant's login comes from claiming the code, never from the QR.
export const PAIRING_QR_VERSION = 2;

export interface PairingQrPayload {
  v: typeof PAIRING_QR_VERSION;
  ssid: string;
  password: string;
  pairing_code: string;
}

export function buildPairingQrPayload(ssid: string, password: string, pairingCode: string): string {
  const payload: PairingQrPayload = {
    v: PAIRING_QR_VERSION,
    ssid,
    password,
    pairing_code: pairingCode,
  };
  return JSON.stringify(payload);
}
