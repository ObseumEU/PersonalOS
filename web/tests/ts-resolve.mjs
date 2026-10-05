// A resolve hook for tests that import app modules: the app imports its own files without an
// extension (Vite resolves them), so a relative import that is not found is retried as "<name>.ts".
export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context);
  } catch (err) {
    if (specifier.startsWith(".") && !/\.[a-z]+$/i.test(specifier)) return next(`${specifier}.ts`, context);
    throw err;
  }
}
