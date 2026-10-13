import globals from "globals";


export const correctnessRules = {
  "constructor-super": "error",
  "for-direction": "error",
  "getter-return": "error",
  "no-async-promise-executor": "error",
  "no-class-assign": "error",
  "no-compare-neg-zero": "error",
  "no-cond-assign": ["error", "except-parens"],
  "no-const-assign": "error",
  "no-constant-binary-expression": "error",
  "no-constant-condition": "error",
  "no-dupe-args": "error",
  "no-dupe-class-members": "error",
  "no-dupe-else-if": "error",
  "no-dupe-keys": "error",
  "no-duplicate-case": "error",
  "no-ex-assign": "error",
  "no-func-assign": "error",
  "no-import-assign": "error",
  "no-loss-of-precision": "error",
  "no-new-native-nonconstructor": "error",
  "no-obj-calls": "error",
  "no-self-assign": "error",
  "no-setter-return": "error",
  "no-this-before-super": "error",
  "no-undef": "error",
  "no-unexpected-multiline": "error",
  "no-unreachable": "error",
  "no-unreachable-loop": "error",
  "no-unsafe-finally": "error",
  "no-unsafe-negation": "error",
  "no-unsafe-optional-chaining": "error",


  "no-use-before-define": ["error", { functions: false, variables: false, classes: false }],
  "require-yield": "error",
  "use-isnan": "error",
  "valid-typeof": "error",
};

export const browserLanguage = {
  ecmaVersion: 2022,
  sourceType: "script",


  globals: { ...globals.browser, module: "readonly" },
};

export default [
  { ignores: ["**/node_modules/**", "**/_vendor/**"] },
  {
    files: ["src/waam_pipeline/**/*.{js,mjs,cjs}", "generated/**/*.js"],
    languageOptions: browserLanguage,
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: correctnessRules,
  },
  {
    files: ["tools/**/*.{js,mjs,cjs}", "eslint.config.mjs"],
    languageOptions: { ecmaVersion: 2022, sourceType: "module", globals: globals.node },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: correctnessRules,
  },
  {
    files: ["tools/**/*.cjs"],
    languageOptions: { sourceType: "commonjs" },
  },
];
