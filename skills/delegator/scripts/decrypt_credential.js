// Расшифровка значений `enc:v1` из ~/.zcode/v2/credentials.json (AES-256-GCM).
// Секрет: env ZCODE_CREDENTIAL_SECRET (эксклюзивно, без fallback), иначе перебор
// дефолтных fallback-секретов приложения
// `zcode-credential-fallback:<platform>:<homedir>:<username>`:
// identity текущего процесса, затем identity владельца пути к credentials.json
// (argv[2]; запуски под другой учётной записью Windows, например из песочницы).
// Значение читается из stdin, расшифрованное пишется в stdout.
// Вызывается только модулем delegator zcode.py; plaintext нигде не логируется.
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const PREFIX = "enc:v1:";
const enc = fs.readFileSync(0, "utf8").trim();
if (!enc.startsWith(PREFIX)) {
  // Значение без префикса считается уже открытым (обратная совместимость).
  process.stdout.write(enc);
  process.exit(0);
}
const [ivB, tagB, dataB] = enc.slice(PREFIX.length).split(".");
if (!ivB || !tagB || !dataB) {
  process.stderr.write("invalid ciphertext format\n");
  process.exit(1);
}

const candidates = [];
const envSecret =
  process.env.ZCODE_CREDENTIAL_SECRET && process.env.ZCODE_CREDENTIAL_SECRET.trim();
if (envSecret) {
  candidates.push(envSecret);
} else {
  let user = "unknown";
  try {
    user = os.userInfo().username;
  } catch {}
  candidates.push(`zcode-credential-fallback:${process.platform}:${os.homedir()}:${user}`);
  const credsPath = process.argv[2];
  if (credsPath) {
    // Макет файла: <home>/.zcode/v2/credentials.json.
    const home = path.dirname(path.dirname(path.dirname(path.resolve(credsPath))));
    candidates.push(`zcode-credential-fallback:${process.platform}:${home}:${path.basename(home)}`);
  }
}

let plaintext = null;
for (const secret of candidates) {
  try {
    const key = crypto.createHash("sha256").update(secret).digest();
    const d = crypto.createDecipheriv("aes-256-gcm", key, Buffer.from(ivB, "base64url"));
    d.setAuthTag(Buffer.from(tagB, "base64url"));
    plaintext = Buffer.concat([d.update(Buffer.from(dataB, "base64url")), d.final()]).toString("utf8");
    break;
  } catch {}
}
if (plaintext === null) {
  process.stderr.write("key mismatch or corrupted ciphertext\n");
  process.exit(1);
}
process.stdout.write(plaintext);
