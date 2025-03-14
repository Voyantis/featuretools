from os import path
from setuptools import find_packages, setup

dirname = path.abspath(path.dirname(__file__))
with open(path.join(dirname, 'README.md')) as f:
    long_description = f.read()

extras_require = {
    'tsfresh': ['featuretools-tsfresh-primitives >= 0.1.0'],
    'update_checker': ['featuretools-update-checker >= 1.0.0'],
    'categorical_encoding': ['categorical-encoding >= 0.2.0'],
    'nlp_primitives': ['nlp-primitives[complete] >= 1.0.0'],
    'autonormalize': ['autonormalize >= 1.0.0'],
    'sklearn_transformer': ['featuretools-sklearn-transformer >= 0.1.1'],
}
extras_require['complete'] = sorted(set(sum(extras_require.values(), [])))

setup(
    name='featuretools',
    version='0.23.1',
    packages=find_packages(),
    description='a framework for automated feature engineering',
    url='http://featuretools.com',
    license='BSD 3-clause',
    author='Feature Labs, Inc.',
    author_email='support@featurelabs.com',
    classifiers=[
         'Development Status :: 3 - Alpha',
         'Intended Audience :: Developers',
         'Programming Language :: Python :: 3',
         'Programming Language :: Python :: 3.6',
         'Programming Language :: Python :: 3.7'
    ],
    install_requires=open('requirements.txt').readlines(),
    python_requires='>=3.7',
    extras_require=extras_require,
    keywords='feature engineering data science machine learning',
    include_package_data=True,
    entry_points={
        'console_scripts': [
            'featuretools = featuretools.__main__:cli'
        ]
    },
    long_description=long_description,
    long_description_content_type='text/markdown'
)
