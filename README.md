<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

AWS code samples are example code that demonstrates practical implementations of AWS services for specific use cases and scenarios.

These application solutions are not supported products in their own right, but educational examples to help our customers use our products for their applications. As our customer, any applications you integrate these examples into should be thoroughly tested, secured, and optimized according to your business's security standards & policies before deploying to production or handling production workloads.

# Guidance for SAP Log Ingestion to AWS Services

This repository provides guidance and a reference implementation for ingesting SAP logs into AWS services.

## Solutions

- **[AWS SAP LogServ Log Forwarder](aws-sap-logserv-forwarder/README.md)** — A serverless (AWS Lambda + SAM) solution that processes SAP RISE LogServ S3 event notifications from SQS, applies filtering, and forwards qualifying log files to a destination S3 bucket. See the [solution README](aws-sap-logserv-forwarder/README.md) for architecture, parameters, and deployment steps.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.


## Notices
Customers are responsible for making their own independent assessment of the information in this Guidance. This Guidance: (a) is for informational purposes only, (b) represents AWS current product offerings and practices, which are subject to change without notice, and (c) does not create any commitments or assurances from AWS and its affiliates, suppliers or licensors. AWS products or services are provided “as is” without warranties, representations, or conditions of any kind, whether express or implied. AWS responsibilities and liabilities to its customers are controlled by AWS agreements, and this Guidance is not part of, nor does it modify, any agreement between AWS and its customers.