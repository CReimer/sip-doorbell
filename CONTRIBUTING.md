# Contributing

Bug reports and pull requests are welcome. Describe the SIP registrar, Home
Assistant installation type and the exact call flow, but redact usernames,
passwords, public addresses, telephone numbers, Call-IDs and unrelated SIP
headers.

Keep the integration limited to local SIP signaling unless a proposed change
explicitly documents and tests any added media or control behavior. New
protocol behavior should include deterministic tests that require no network
or SIP hardware.

Run the validation suite before submitting a pull request:

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -t .
```

Contributions submitted for inclusion are licensed under Apache-2.0.
