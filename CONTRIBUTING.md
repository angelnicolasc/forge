# Contributing to Forge Check

Forge Check is maintained by Ángel. Use a [GitHub issue](https://github.com/angelnicolasc/forge/issues) to propose a change, report a documentation problem, or share a small reproduction. For security reports, follow the [security policy](SECURITY.md).

For a result you did not expect, include the version from `forge --version`, your platform, a synthetic case, the expected verdict, and the exit code or report you received. The [check guide](docs/check.md) describes the inputs. Please omit customer records, prompts and credentials.

## Maintainer workflow

The maintainer integrates code through a pull request and signs each commit in the candidate series with the project's registered SSH signing key. Describe the behavior change in the title and give the command or test that exercises it. For changes to the standalone CLI, build the wheel from the candidate commit and run the support cases against the installed executable. For documentation, check the rendered page and run the commands that changed.

From the repository root, activate a fresh virtual environment with Python 3.11 or newer, then run:

```sh
python -m pip install ./standalone/forge-check
python -I standalone/forge-check/tests/test_check_cli.py -v
```

The suite invokes the environment's `forge` executable from temporary directories and checks the JSON reports it writes.

The current package lives in [standalone/forge-check](standalone/forge-check/). The [support fixture](examples/check-support/) provides a small set of inputs. The [historical harness](packages/README.md) has its own packages and tests.

Forge is licensed under [Apache 2.0](LICENSE).
