/** Browser-side helpers for the WebAuthn JSON <-> ArrayBuffer conversion. */

export type CredentialDescriptorJSON = { id: string; type: string };

export type CreationOptionsJSON = {
  rp: { name: string; id?: string };
  user: { id: string; name: string; displayName: string };
  challenge: string;
  pubKeyCredParams: Array<{ type: string; alg: number }>;
  timeout?: number;
  excludeCredentials?: CredentialDescriptorJSON[];
  authenticatorSelection?: Record<string, unknown>;
  attestation?: string;
};

export type RequestOptionsJSON = {
  challenge: string;
  timeout?: number;
  rpId?: string;
  allowCredentials?: CredentialDescriptorJSON[];
  userVerification?: string;
};

export type WebAuthnCredentialJSON = {
  id: string;
  rawId: string;
  type: string;
  response: Record<string, string | null>;
};

export function webauthnSupported(): boolean {
  return typeof window !== "undefined" && "PublicKeyCredential" in window && !!navigator.credentials;
}

function fromBase64url(value: string): ArrayBuffer {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes.buffer;
}

function toBase64url(value: ArrayBuffer | ArrayBufferView | null | undefined): string | null {
  if (!value) return null;
  const bytes =
    value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function creationOptions(json: CreationOptionsJSON): PublicKeyCredentialCreationOptions {
  const options = {
    ...json,
    challenge: fromBase64url(json.challenge),
    user: { ...json.user, id: fromBase64url(json.user.id) },
    excludeCredentials: (json.excludeCredentials ?? []).map((credential) => ({
      ...credential,
      id: fromBase64url(credential.id),
    })),
  };
  return options as unknown as PublicKeyCredentialCreationOptions;
}

export function requestOptions(json: RequestOptionsJSON): PublicKeyCredentialRequestOptions {
  const options = {
    ...json,
    challenge: fromBase64url(json.challenge),
    allowCredentials: (json.allowCredentials ?? []).map((credential) => ({
      ...credential,
      id: fromBase64url(credential.id),
    })),
  };
  return options as unknown as PublicKeyCredentialRequestOptions;
}

type WebAuthnResponseLike = {
  clientDataJSON: ArrayBuffer;
  attestationObject?: ArrayBuffer;
  authenticatorData?: ArrayBuffer;
  signature?: ArrayBuffer;
  userHandle?: ArrayBuffer | null;
};

export function serializeCredential(credential: PublicKeyCredential): WebAuthnCredentialJSON {
  const response = credential.response as unknown as WebAuthnResponseLike;
  const serialized: Record<string, string | null> = {
    clientDataJSON: toBase64url(response.clientDataJSON),
  };
  if (response.attestationObject !== undefined) {
    serialized.attestationObject = toBase64url(response.attestationObject);
  } else {
    serialized.authenticatorData = toBase64url(response.authenticatorData);
    serialized.signature = toBase64url(response.signature);
    serialized.userHandle = toBase64url(response.userHandle);
  }
  return {
    id: credential.id,
    rawId: toBase64url(credential.rawId) ?? credential.id,
    type: credential.type,
    response: serialized,
  };
}
