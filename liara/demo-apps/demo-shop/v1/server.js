// Demo shop v1 — healthy. A tiny Persian store with zero dependencies (Node's own http module).
// Logs every request as: METHOD path status ms   (the SRE agent reads these from Liara's app logs)
// Needs PAYMENT_API_URL; /checkout fails with a TypeError if it's missing (the "missing env" fault).

const http = require("node:http");

const PORT = process.env.PORT || 3000;
const products = [
  { id: 1, name: "هدفون بی‌سیم", price: 1850000 },
  { id: 2, name: "شارژر ۶۵ وات", price: 690000 },
  { id: 3, name: "کابل تایپ‌سی", price: 145000 },
];

const toman = (n) => n.toLocaleString("fa-IR") + " تومان";

function page(title, body) {
  return `<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>${title}</title>
<body style="font-family:Tahoma,sans-serif;max-width:560px;margin:24px auto;padding:0 16px">${body}</body></html>`;
}

function home() {
  const items = products.map((p) =>
    `<li>${p.name} — ${toman(p.price)} <a href="/checkout?id=${p.id}">خرید</a></li>`).join("");
  return page("فروشگاه دمو", `<h1>فروشگاه دمو</h1><ul>${items}</ul>`);
}

function checkout(url) {
  const product = products.find((p) => p.id === Number(url.searchParams.get("id"))) || products[0];
  const gateway = new URL(process.env.PAYMENT_API_URL);       // TypeError if PAYMENT_API_URL is not set
  return page("پرداخت", `<h1>پرداخت</h1><p>${product.name}: ${toman(product.price)}</p>
<p>درگاه: ${gateway.host}</p><a href="/">بازگشت</a>`);
}

const routes = {
  "/": () => [200, "text/html", home()],
  "/checkout": (url) => [200, "text/html", checkout(url)],
  "/api/products": () => [200, "application/json", JSON.stringify(products)],
  "/health": () => [200, "application/json", '{"ok":true}'],
};

http.createServer((req, res) => {
  const t0 = process.hrtime.bigint();
  const url = new URL(req.url, "http://localhost");
  let [status, type, body] = [404, "text/plain", "Not Found"];
  try {
    const route = routes[url.pathname];
    if (route) [status, type, body] = route(url);
  } catch (err) {
    console.error(err.stack);                                  // like Express: stack trace to stderr
    [status, type, body] = [500, "text/plain", "Internal Server Error"];
  }
  res.writeHead(status, { "content-type": `${type}; charset=utf-8` });
  res.end(body);
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  console.log(`${req.method} ${url.pathname} ${status} ${ms.toFixed(1)}ms`);
}).listen(PORT, "0.0.0.0", () => console.log(`demo-shop v1 listening on ${PORT}`));
