import { compileFromFile } from 'json-schema-to-typescript';
import { writeFile } from 'node:fs/promises';
const output = await compileFromFile('../../shared/schemas/event.schema.json', {bannerComment:'/* Generated from shared/schemas/event.schema.json. Do not edit. */',additionalProperties:false});
await writeFile('src/events.d.ts', output);
