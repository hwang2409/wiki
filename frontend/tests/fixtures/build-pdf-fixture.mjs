// Minimal handcrafted PDF builder for artifact fixtures.
// Not a general-purpose writer — good enough for a fixed set of pages showing
// known text (so text-selection + cmd+f tests have real content to match).

function escapePdfString(text) {
  return text.replace(/[\\()]/g, (match) => `\\${match}`);
}

function toBytes(str) {
  return Buffer.from(str, "latin1");
}

export function buildFixturePdf(pageTexts) {
  if (!Array.isArray(pageTexts) || pageTexts.length === 0) {
    throw new Error("buildFixturePdf requires a non-empty array of page strings");
  }
  const objects = [];
  const push = (body) => {
    objects.push(body);
    return objects.length; // 1-based object id
  };

  const catalogId = push(null);
  const pagesId = push(null);
  const fontId = push(`<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>`);
  const pageIds = [];
  const contentIds = [];
  for (let index = 0; index < pageTexts.length; index += 1) {
    contentIds.push(push(null));
    pageIds.push(push(null));
  }

  objects[catalogId - 1] = `<< /Type /Catalog /Pages ${pagesId} 0 R >>`;
  objects[pagesId - 1] = `<< /Type /Pages /Count ${pageTexts.length} /Kids [ ${pageIds
    .map((id) => `${id} 0 R`)
    .join(" ")} ] >>`;

  pageTexts.forEach((text, index) => {
    const encoded = escapePdfString(text);
    const stream = `BT /F1 24 Tf 72 720 Td (${encoded}) Tj ET\n`;
    const streamBytes = Buffer.byteLength(stream, "latin1");
    objects[contentIds[index] - 1] =
      `<< /Length ${streamBytes} >>\nstream\n${stream}endstream`;
    objects[pageIds[index] - 1] = [
      `<< /Type /Page /Parent ${pagesId} 0 R /MediaBox [0 0 612 792]`,
      `/Contents ${contentIds[index]} 0 R`,
      `/Resources << /Font << /F1 ${fontId} 0 R >> >> >>`,
    ].join(" ");
  });

  const chunks = [];
  chunks.push(toBytes("%PDF-1.4\n%\xE2\xE3\xCF\xD3\n"));
  const offsets = new Array(objects.length + 1).fill(0);
  let cursor = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  for (let index = 0; index < objects.length; index += 1) {
    const id = index + 1;
    offsets[id] = cursor;
    const body = `${id} 0 obj\n${objects[index]}\nendobj\n`;
    const buffer = toBytes(body);
    chunks.push(buffer);
    cursor += buffer.length;
  }
  const xrefStart = cursor;
  let xref = `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (let id = 1; id <= objects.length; id += 1) {
    xref += `${offsets[id].toString().padStart(10, "0")} 00000 n \n`;
  }
  xref += `trailer\n<< /Size ${objects.length + 1} /Root ${catalogId} 0 R >>\n`;
  xref += `startxref\n${xrefStart}\n%%EOF\n`;
  chunks.push(toBytes(xref));
  return Buffer.concat(chunks);
}
